// ZoneEditor.tsx — el editor de zonas poligonales, superpuesto al feed.
//
// LA EXCEPCION DELIBERADA A "EL CLIENTE NO DIBUJA". Desde el paso 3 (2026-08-26) el
// cliente no contiene ni una linea que dibuje una caja: el backend compone el frame y
// aca solo se pinta. Este archivo dibuja. No es una regresion, y la distincion es
// exacta: no dibuja un RESULTADO DEL MODELO, dibuja un CONTROL. Mientras el usuario
// arrastra un vertice no se puede ir y volver al backend a la velocidad del arrastre;
// al CONFIRMAR, el poligono pasa a dibujarlo el backend con PolygonZoneAnnotator y
// este componente deja de mostrarlo.
//
// EL RIESGO CONCRETO ES EL SALTO: si las dos representaciones no coincidieran, el
// poligono "saltaria" al confirmarse. Lo evita que ambas consuman LAS MISMAS
// coordenadas normalizadas [0,1] — el estado es normalizado de punta a punta, y cada
// lado lo multiplica por el tamano que le toca (aca la caja en pantalla, alla el frame
// en pixeles). Nunca se guarda un pixel.
//
// SOBRE EL viewBox="0 0 1 1" QUE PROPONIA EL SPEC: se probo y se descarto. La idea era
// no escribir una sola multiplicacion, dejando que el navegador escale. Funciona para
// el poligono, pero TODO lo que mide en pixeles de PANTALLA —el radio de un vertice, el
// grosor del trazo, el iman de cierre— pasa a necesitar una contra-escala, porque en
// ese sistema una unidad es el frame entero. La aritmetica que se ahorra en el
// poligono se paga con intereses en los mangos, que es justamente la parte que hay que
// poder tocar con el mouse. Asi que el SVG trabaja en pixeles de la caja y la
// conversion es una multiplicacion por punto, en un solo lugar (`aPx`).

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { useStreamStore } from '@/features/inference/store/streamStore';
import { useWorkspaceStore } from '../store/workspaceStore';
import {
  MAX_ZONAS,
  MIN_VERTICES,
  cajaDelCanvas,
  nuevoIdDeZona,
  zonaValida,
  type Punto,
} from '@/features/inference/services/geometry';

interface ZoneEditorProps {
  canvasRef: RefObject<HTMLCanvasElement | null>;
}

// Radio del vertice y del "iman" que cierra el poligono, en px de PANTALLA: son
// objetivos para el mouse, asi que tienen que medir lo mismo sobre un frame de 320 px
// que sobre uno de 4K.
const RADIO_VERTICE = 6;
const IMAN_CIERRE = 14;

interface Caja {
  left: number;
  top: number;
  width: number;
  height: number;
}

export function ZoneEditor({ canvasRef }: ZoneEditorProps) {
  const modo = useStreamStore((s) => s.modoZona);
  const setModo = useStreamStore((s) => s.setModoZona);
  const zonas = useStreamStore((s) => s.zonas);
  const setZonas = useStreamStore((s) => s.setZonas);
  const color = useWorkspaceStore((s) => s.drawSettings.zoneColor);

  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  // Poligono en construccion, y donde esta el cursor (la "banda elastica" que muestra
  // hacia donde iria el proximo lado).
  const [enCurso, setEnCurso] = useState<Punto[]>([]);
  const [cursor, setCursor] = useState<Punto | null>(null);
  // Vertice que se esta arrastrando: [indice de zona, indice de vertice].
  const [arrastre, setArrastre] = useState<[number, number] | null>(null);
  // Caja de contenido del canvas, relativa al contenedor del workspace.
  const [caja, setCaja] = useState<Caja | null>(null);

  const activo = modo !== 'off';

  // ── La caja se MIDE, no se asume ──────────────────────────────────────────
  // El canvas se centra con max-h/max-w y conserva el aspecto del bitmap, mientras que
  // el contenedor ocupa todo el workspace. Sin esto el SVG quedaria estirado sobre las
  // bandas vacias y cada click caeria corrido.
  const medir = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap || !canvas.width || !canvas.height) return;
    setCaja(cajaDelCanvas(canvas, wrap));
  }, [canvasRef]);

  useLayoutEffect(() => {
    if (!activo) return;
    medir();
    const canvas = canvasRef.current;
    if (!canvas) return;
    // Se observa el CANVAS y no la ventana: cambiar de fuente cambia el tamano del
    // bitmap, y con el la caja renderizada, sin que la ventana se mueva.
    const obs = new ResizeObserver(medir);
    obs.observe(canvas);
    window.addEventListener('resize', medir);
    return () => {
      obs.disconnect();
      window.removeEventListener('resize', medir);
    };
  }, [activo, medir, canvasRef]);

  /** Click del usuario -> coordenadas normalizadas. Toda la conversion de entrada es esto. */
  const normalizar = useCallback((ev: { clientX: number; clientY: number }): Punto => {
    const svg = svgRef.current;
    if (!svg) return [0, 0];
    const r = svg.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width;
    const y = (ev.clientY - r.top) / r.height;
    // Se recorta igual que del lado del backend: arrastrar un vertice mas alla del
    // borde es un gesto legitimo ("que la zona llegue hasta el borde").
    return [Math.min(Math.max(x, 0), 1), Math.min(Math.max(y, 0), 1)];
  }, []);

  const cerrar = useCallback(() => {
    if (!zonaValida(enCurso)) return;
    setZonas([...zonas, { id: nuevoIdDeZona(zonas), puntos: enCurso }]);
    setEnCurso([]);
    setCursor(null);
    // Queda en 'editando' y no en 'off': recien dibujada es cuando mas ganas hay de
    // corregir un vertice.
    setModo('editando');
  }, [enCurso, zonas, setZonas, setModo]);

  // Esc cancela lo que se esta dibujando; Enter lo cierra si ya tiene forma.
  useEffect(() => {
    if (modo !== 'dibujando') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setEnCurso([]);
        setCursor(null);
        setModo('off');
      } else if (e.key === 'Enter') {
        cerrar();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [modo, cerrar, setModo]);

  // El wrapper existe SIEMPRE (hay que poder medir contra el), pero mientras no se
  // edita no captura un solo evento ni dibuja nada: la zona confirmada la pinta el
  // backend dentro del frame.
  if (!activo || !caja) {
    return <div ref={wrapRef} className="pointer-events-none absolute inset-0" />;
  }

  /** Punto normalizado -> px de la caja. La unica conversion de salida. */
  const aPx = (p: Punto): [number, number] => [p[0] * caja.width, p[1] * caja.height];
  const comoPath = (pts: Punto[]) => pts.map((p) => aPx(p).join(',')).join(' ');

  function distanciaPx(a: Punto, b: Punto): number {
    const [ax, ay] = aPx(a);
    const [bx, by] = aPx(b);
    return Math.hypot(ax - bx, ay - by);
  }

  function onPointerDown(ev: React.PointerEvent) {
    if (modo !== 'dibujando' || zonas.length >= MAX_ZONAS) return;
    const p = normalizar(ev);
    // Cerrar tocando el primer vertice es el gesto estandar de cualquier editor de
    // poligonos; el boton de la columna hace lo mismo para quien no lo conozca.
    if (enCurso.length >= MIN_VERTICES && distanciaPx(p, enCurso[0]) <= IMAN_CIERRE) {
      cerrar();
      return;
    }
    setEnCurso([...enCurso, p]);
  }

  function onPointerMove(ev: React.PointerEvent) {
    if (modo === 'dibujando') {
      if (enCurso.length > 0) setCursor(normalizar(ev));
      return;
    }
    if (!arrastre) return;
    const [zi, vi] = arrastre;
    const p = normalizar(ev);
    setZonas(
      zonas.map((z, i) =>
        i !== zi ? z : { ...z, puntos: z.puntos.map((q, j) => (j === vi ? p : q)) },
      ),
    );
  }

  const enCursoCerrable = zonaValida(enCurso);
  const lleno = zonas.length >= MAX_ZONAS;

  return (
    <div ref={wrapRef} className="absolute inset-0">
      <svg
        ref={svgRef}
        // En px de la caja, no en un viewBox de 1x1: ver el encabezado del archivo.
        viewBox={`0 0 ${caja.width} ${caja.height}`}
        className="absolute"
        style={{
          left: caja.left,
          top: caja.top,
          width: caja.width,
          height: caja.height,
          // pointer-events solo mientras se edita: overlayRoot es pointer-events-none
          // justamente para no robarle el cursor al resto de la app.
          pointerEvents: 'auto',
          cursor: modo === 'dibujando' ? (lleno ? 'not-allowed' : 'crosshair') : 'default',
          touchAction: 'none',
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={() => setArrastre(null)}
        onPointerLeave={() => {
          setArrastre(null);
          setCursor(null);
        }}
      >
        {/* Zonas ya confirmadas: el backend las pinta DENTRO del frame, asi que aca van
            solo los mangos. Repintar el poligono seria dibujar dos veces lo mismo, y
            cualquier desfasaje de medio pixel se leeria como un borde doble. */}
        {modo === 'editando' &&
          zonas.map((z, zi) =>
            z.puntos.map((p, vi) => {
              const [cx, cy] = aPx(p);
              return (
                <circle
                  key={`${z.id}-${vi}`}
                  cx={cx}
                  cy={cy}
                  r={RADIO_VERTICE}
                  fill={color}
                  stroke="#000"
                  strokeWidth={1}
                  style={{ cursor: 'grab' }}
                  onPointerDown={(e) => {
                    e.stopPropagation();
                    e.currentTarget.setPointerCapture(e.pointerId);
                    setArrastre([zi, vi]);
                  }}
                />
              );
            }),
          )}

        {/* El poligono en construccion: esto SI lo dibuja el cliente (ver encabezado).
            Punteado y translucido a proposito — se lee como "todavia no es una zona". */}
        {modo === 'dibujando' && enCurso.length > 0 && (
          <>
            <polyline
              points={comoPath([...enCurso, ...(cursor ? [cursor] : [])])}
              fill={enCursoCerrable ? color : 'none'}
              fillOpacity={0.12}
              stroke={color}
              strokeWidth={2}
              strokeDasharray="6 4"
              strokeLinejoin="round"
            />
            {enCurso.map((p, i) => {
              const [cx, cy] = aPx(p);
              // El primer vertice se agranda cuando ya se puede cerrar: es el objetivo
              // del gesto, y crecer es como el editor dice "toca aca para terminar".
              const primeroActivo = i === 0 && enCursoCerrable;
              return (
                <circle
                  key={i}
                  cx={cx}
                  cy={cy}
                  r={primeroActivo ? RADIO_VERTICE + 2 : RADIO_VERTICE - 2}
                  fill={primeroActivo ? color : '#000'}
                  stroke={color}
                  strokeWidth={1.5}
                />
              );
            })}
          </>
        )}
      </svg>

      {/* La instruccion va al pie del FEED y no en la columna: mientras se dibuja los
          ojos estan sobre la imagen, y el gesto de cerrar un poligono no es obvio. */}
      {modo === 'dibujando' && (
        <p
          className="pointer-events-none absolute left-1/2 -translate-x-1/2 rounded-[var(--radius-sm)] border border-border bg-surface/90 px-2.5 py-1 font-mono text-[10px] tracking-wide text-fg-subtle"
          style={{ top: caja.top + caja.height - 34 }}
        >
          {lleno
            ? `MAXIMO ${MAX_ZONAS} ZONAS`
            : enCursoCerrable
              ? 'CLIC EN EL PRIMER PUNTO O ENTER PARA CERRAR · ESC CANCELA'
              : `MARCA AL MENOS ${MIN_VERTICES} PUNTOS · ESC CANCELA`}
        </p>
      )}
    </div>
  );
}
