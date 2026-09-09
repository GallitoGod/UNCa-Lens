// ZoneSettings.tsx — panel de Zonas (columna derecha de Inferencia).
//
// Una zona responde, frame a frame, cuantas detecciones caen adentro de un poligono.
// Sirve a los DOS objetivos del sistema: acota la evaluacion a la region que importa
// ("ignora la vereda de enfrente y decime que tal anda el modelo en el carril"), y
// hace visible para alguien de afuera lo que el modelo esta haciendo.
//
// POR QUE ES UNA SECCION APARTE de Render y de Seguimiento: Render gobierna como se
// PINTA una deteccion y Seguimiento como se sigue un objeto A LO LARGO DEL TIEMPO.
// Esto es otra cosa: geometria de LA ESCENA. Y se nota en donde vive cada cosa — el
// color y el anclaje persisten (son del usuario), el POLIGONO no (muere con la fuente,
// porque describe la escena que se esta mirando). Ver services/geometry.ts.
//
// Cuelga de la MISMA condicion que Render y Seguimiento (`panelDeRenderAplica`): con
// un clasificador no hay geometria sobre la que contar nada.

import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';
import type { DrawSettings, ZoneAnchor } from '@/features/vision-workspace/services/types';
import { Interruptor } from '@/shared/ui/Interruptor';
import { pushDrawSettings } from '../api/drawSettings';
import { useStreamStore } from '../store/streamStore';
import { MAX_ZONAS } from '../services/geometry';
import { Color, Opcion } from './RenderSettings';
import { aplicarDependencias, seguimientoAplicaA } from './TrackingSettings';
import { cn } from '@/shared/ui/cn';

// Que punto de la caja decide si una deteccion esta adentro. No es cosmetico: cambia
// el CONTEO, y cual es el correcto depende de desde donde mira la camara. Se expone
// justamente porque ver como cambia el numero al cambiar el anclaje es la clase de
// cosa que el objetivo educativo del sistema quiere hacer visible.
export const ANCLAJES: { key: ZoneAnchor; label: string; hint: string }[] = [
  {
    key: 'centro',
    label: 'Centro',
    hint: 'El centro de la caja. Es el unico que se comporta igual en vista aerea y en vista de calle.',
  },
  {
    key: 'inferior',
    label: 'Base',
    hint: 'El borde inferior, donde el objeto toca el piso. Lo correcto para una camara de calle: un auto esta en el carril donde apoyan sus ruedas.',
  },
];

/**
 * Si el panel tiene algo que gobernar para la fuente activa.
 *
 * Sin fuente NO: una zona se dibuja SOBRE un frame, y sin frame no hay nada sobre que
 * dibujar ni con que medir el aspecto. Es distinto de Render y Seguimiento, que sin
 * fuente se dejan operables porque son preferencias que se pueden dejar listas de
 * antemano — un poligono no se puede preparar en el aire.
 */
export function zonasAplicanA(sourceKind: string): boolean {
  return sourceKind !== 'none';
}

export function ZoneSettings() {
  const drawSettings = useWorkspaceStore((s) => s.drawSettings);
  const setDrawSettings = useWorkspaceStore((s) => s.setDrawSettings);
  const zonas = useStreamStore((s) => s.zonas);
  const setZonas = useStreamStore((s) => s.setZonas);
  const modo = useStreamStore((s) => s.modoZona);
  const setModo = useStreamStore((s) => s.setModoZona);
  const sourceKind = useStreamStore((s) => s.source.kind);
  const resendStill = useStreamStore((s) => s.resendStill);
  const resetZona = useStreamStore((s) => s.resetZona);

  const operable = zonasAplicanA(sourceKind);
  const lleno = zonas.length >= MAX_ZONAS;
  // El acumulado cuenta objetos DISTINTOS, o sea que necesita identidad, o sea
  // seguimiento — y el seguimiento solo existe sobre una secuencia. Una foto suelta
  // no la tiene, asi que ahi el toggle queda inerte y hay que decirlo.
  const puedeAcumular = operable && seguimientoAplicaA(sourceKind);

  // Los ajustes de zona son del usuario y viajan por POST /config/draw, igual que los
  // colores de caja. La GEOMETRIA no pasa por aca: va por el WebSocket (useVisionSession).
  function aplicar(patch: Partial<DrawSettings>) {
    // Por aplicarDependencias porque 'zoneTotal' cuelga del seguimiento igual que el
    // suavizado y las trazas: pedirlo lo prende. La regla la fuerza el backend en su
    // unica puerta de escritura; replicarla aca evita que el panel parpadee esperando
    // la respuesta, y ambos convergen porque es la misma regla.
    const efectivo = aplicarDependencias(patch);
    setDrawSettings(efectivo);
    pushDrawSettings({ ...drawSettings, ...efectivo });
    // Con una imagen fija no hay frame siguiente donde se vea el cambio.
    resendStill();
  }

  function borrar(id: string) {
    const quedan = zonas.filter((z) => z.id !== id);
    setZonas(quedan);
    if (quedan.length === 0 && modo === 'editando') setModo('off');
  }

  return (
    <div className="flex flex-col gap-2.5">
      {!operable && (
        <p className="px-0.5 text-[11px] leading-snug text-label">
          Elegi una fuente primero: la zona se dibuja sobre el frame.
        </p>
      )}

      <div className="flex gap-1.5">
        <button
          type="button"
          disabled={!operable || lleno}
          title={
            lleno
              ? `Maximo ${MAX_ZONAS} zonas`
              : 'Marca los vertices sobre el feed. Clic en el primero para cerrar.'
          }
          onClick={() => setModo(modo === 'dibujando' ? 'off' : 'dibujando')}
          className={cn(
            'flex-1 rounded-[var(--radius-sm)] border px-2.5 py-1.5 text-xs font-medium',
            'transition-colors duration-150 focus-visible:outline-none active:scale-[0.98]',
            'disabled:cursor-not-allowed disabled:opacity-40',
            modo === 'dibujando'
              ? 'border-accent-border bg-accent-soft text-accent'
              : 'border-border bg-control text-fg-subtle hover:border-border-strong hover:text-fg',
          )}
        >
          {modo === 'dibujando' ? 'Cancelar' : 'Dibujar zona'}
        </button>

        {/* Editar los vertices es un MODO y no una accion: mientras esta prendido el
            editor le saca el cursor al resto del feed, asi que tiene que verse que
            esta activo y poder apagarse. */}
        <button
          type="button"
          disabled={!operable || zonas.length === 0}
          title="Muestra los vertices de las zonas para arrastrarlos."
          onClick={() => setModo(modo === 'editando' ? 'off' : 'editando')}
          className={cn(
            'rounded-[var(--radius-sm)] border px-2.5 py-1.5 text-xs font-medium',
            'transition-colors duration-150 focus-visible:outline-none active:scale-[0.98]',
            'disabled:cursor-not-allowed disabled:opacity-40',
            modo === 'editando'
              ? 'border-accent-border bg-accent-soft text-accent'
              : 'border-border bg-control text-fg-subtle hover:border-border-strong hover:text-fg',
          )}
        >
          Vertices
        </button>
      </div>

      {/* La lista es la unica forma de saber que zonas hay sin mirar el feed, y la
          unica de borrar una sola. El numero de cada una es el mismo id que el backend
          devuelve en el ack. */}
      {zonas.length > 0 && (
        <ul className="flex flex-col gap-1">
          {zonas.map((z) => (
            <li
              key={z.id}
              className="flex items-center gap-2 rounded-[var(--radius-sm)] border border-border bg-control px-2.5 py-1.5"
            >
              <span
                aria-hidden
                className="size-2 shrink-0 rounded-[2px]"
                style={{ backgroundColor: drawSettings.zoneColor }}
              />
              <span className="text-xs text-fg-subtle">{z.id}</span>
              <span className="ml-auto font-mono text-[10px] text-label">
                {z.puntos.length} pts
              </span>
              {/* Solo con el acumulado prendido: sin cuenta no hay nada que poner en
                  cero, y un boton inerte en una fila de 3 iconos es ruido. */}
              {drawSettings.zoneTotal && (
                <button
                  type="button"
                  onClick={() => resetZona(z.id)}
                  aria-label={`Poner en cero la cuenta de ${z.id}`}
                  title="Poner la cuenta en cero (el poligono no se toca)"
                  className="text-label transition-colors hover:text-accent"
                >
                  <svg viewBox="0 0 12 12" aria-hidden className="size-3">
                    <path
                      d="M9.5 6a3.5 3.5 0 1 1-1.03-2.47"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                    />
                    <path d="M9.6 1.6 L9.6 4.1 L7.1 4.1" fill="none" stroke="currentColor"
                          strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              )}
              <button
                type="button"
                onClick={() => borrar(z.id)}
                aria-label={`Borrar zona ${z.id}`}
                title="Borrar esta zona"
                className="text-label transition-colors hover:text-danger"
              >
                <svg viewBox="0 0 12 12" aria-hidden className="size-3">
                  <path
                    d="M3 3 L9 9 M9 3 L3 9"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* El acumulado va DESPUES de la lista y antes del anclaje: primero que zonas
          hay, despues que cuentan y como. */}
      <Interruptor
        label="Acumulado"
        hint={
          puedeAcumular
            ? 'Cuenta cuantos objetos DISTINTOS pasaron por la zona, ademas de cuantos hay ahora: el cartel dice "ahora / total". Necesita seguimiento (lo prende solo), porque sin identidad el mismo auto durante 30 frames serian 30 autos.'
            : 'Solo con camara o video: contar objetos distintos necesita seguirlos entre frames, y una imagen fija es un frame suelto.'
        }
        on={drawSettings.zoneTotal}
        disabled={!puedeAcumular}
        onToggle={() => aplicar({ zoneTotal: !drawSettings.zoneTotal })}
      />

      <div>
        <p className="mb-1.5 px-0.5 text-[11px] text-label">Cuenta por</p>
        <div className="grid grid-cols-2 gap-1.5" role="group" aria-label="Anclaje de conteo">
          {ANCLAJES.map((a) => (
            <Opcion
              key={a.key}
              label={a.label}
              hint={a.hint}
              activo={drawSettings.zoneAnchor === a.key}
              onClick={() => aplicar({ zoneAnchor: a.key })}
            />
          ))}
        </div>
      </div>

      <Color
        label="Zona"
        value={drawSettings.zoneColor}
        onChange={(v) => aplicar({ zoneColor: v })}
      />
    </div>
  );
}
