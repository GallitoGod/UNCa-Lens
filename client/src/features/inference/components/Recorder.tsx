// Recorder.tsx — la barra de transporte: UN boton que captura la sesion, y dos
// interruptores que deciden que mitades captura (el VIDEO de salida y las DETECCIONES).
//
// Eran dos botones hermanos e independientes hasta el 2026-09-10. Se unificaron porque
// el caso comun es querer las dos mitades DEL MISMO TRAMO, y con dos clicks el par no se
// podia alinear en el tiempo ni relacionar por el nombre. El motivo largo esta en
// useCaptura.ts, que es donde vive la logica.
//
// LO QUE NO SE UNIFICO SON LOS BADGES, y es deliberado: el video es local y el volcado
// es del backend, asi que pueden fallar por separado. Un indicador unico tendria que
// representar dos verdades. El gesto es uno; el estado sigue diciendo la verdad por
// mitades.

import { type RefObject } from 'react';
import { cn } from '@/shared/ui/cn';
import { Badge } from '@/shared/ui/Badge';
import { useCaptura } from '../hooks/useCaptura';

export function Recorder({ canvasRef }: { canvasRef: RefObject<HTMLCanvasElement | null> }) {
  const {
    activa,
    grabandoVideo,
    exportando,
    quiere,
    setQuiere,
    formato,
    ultimasFilas,
    error,
    puedeArrancar,
    start,
    stop,
  } = useCaptura(canvasRef);

  // Sin nada tildado el boton no puede hacer nada. Se DESHABILITA y el rotulo dice por
  // que, en vez de tragarse el click o de forzar que la ultima mitad no se pueda apagar:
  // un control que ignora lo que le piden es peor que uno que explica que no puede.
  const nadaTildado = !quiere.video && !quiere.datos;

  return (
    <div className="flex items-center gap-4">
      {/* ── El gesto ── */}
      <button
        type="button"
        onClick={activa ? stop : start}
        disabled={!activa && !puedeArrancar}
        aria-label={activa ? 'Terminar la captura' : 'Iniciar la captura'}
        aria-pressed={activa}
        title={
          nadaTildado
            ? 'Elegi al menos una cosa para capturar'
            : puedeArrancar || activa
              ? 'Captura el tramo: video de salida y/o detecciones, con el mismo nombre'
              : 'Solo con camara o video'
        }
        className={cn(
          'grid size-9 shrink-0 place-items-center rounded-full border bg-control',
          'transition-colors duration-150 focus-visible:outline-none active:scale-95',
          'disabled:cursor-not-allowed disabled:opacity-40',
          activa ? 'border-[rgba(255,77,79,0.35)]' : 'border-border hover:border-border-strong',
        )}
      >
        <span
          className={cn(
            'size-3 bg-danger transition-all duration-150',
            activa ? 'rounded-[2px]' : 'rounded-full',
          )}
        />
      </button>

      {/* ── Que esta pasando DE VERDAD ── */}
      <div className="flex min-w-0 items-center gap-2">
        {grabandoVideo && <Badge variant="rec">REC</Badge>}
        {exportando && <Badge variant="rec">DATOS</Badge>}
        {!grabandoVideo && !exportando && (
          <span className="text-[11px] text-fg-subtle">
            {nadaTildado ? (
              'Nada que capturar'
            ) : (
              <>
                {activa ? 'Arrancando' : 'Capturar sesion'}
                {/* El formato se muestra porque se ELIGE segun lo que el runtime
                    soporte (mp4 si puede, webm si no): decir cual salio evita la
                    sorpresa. */}
                {quiere.video && formato && (
                  <span className="ml-1 font-mono text-[10px] text-label">.{formato}</span>
                )}
                {/* Cuantas filas salieron: sin esto, un volcado sin detecciones y uno
                    exitoso se ven exactamente igual (en los dos casos "no pasa nada").
                    Va atado a que 'Datos' este tildado, y eso lo destapo la verificacion:
                    despues de una captura de SOLO VIDEO el cartel seguia mostrando las
                    filas del volcado ANTERIOR, o sea un numero que no era de la captura
                    que se acababa de hacer. */}
                {quiere.datos && ultimasFilas != null && (
                  <span className="ml-1.5 font-mono text-[10px] text-label">
                    {ultimasFilas} filas
                  </span>
                )}
              </>
            )}
          </span>
        )}
      </div>

      <span aria-hidden className="h-6 w-px bg-border" />

      {/* ── Que capturar. Se congela mientras hay captura: cambiarlo a mitad de camino
             partiria el par que la unificacion existe para mantener junto. ── */}
      <div className="flex items-center gap-1.5">
        <Mitad
          activa={quiere.video}
          disabled={activa}
          onClick={() => setQuiere('video', !quiere.video)}
          title={`Graba el feed compuesto a .${formato ?? '?'}`}
        >
          Video
        </Mitad>
        <Mitad
          activa={quiere.datos}
          disabled={activa}
          onClick={() => setQuiere('datos', !quiere.datos)}
          title="Guarda cada deteccion de cada frame en un .json. Con el seguimiento prendido incluye el #id, que es lo que permite reconstruir el recorrido de cada objeto."
        >
          Datos
        </Mitad>
      </div>

      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

// Interruptor chico de "esta mitad entra en la captura". Es un boton con `aria-pressed`
// y no un checkbox por coherencia con el resto de la piel (Interruptor, Opcion): en esta
// app prendido/apagado se dice con el color de acento, no con un tilde.
function Mitad({
  activa,
  disabled,
  onClick,
  title,
  children,
}: {
  activa: boolean;
  disabled?: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={activa}
      title={title}
      className={cn(
        'rounded-[var(--radius-sm)] border px-2.5 py-1',
        'font-mono text-[10px] font-bold uppercase tracking-wide',
        'transition-colors duration-150 focus-visible:outline-none',
        'disabled:cursor-not-allowed disabled:opacity-40',
        activa
          ? 'border-accent-border bg-accent-soft text-accent'
          : 'border-border bg-control text-fg-subtle hover:border-border-strong',
      )}
    >
      {children}
    </button>
  );
}
