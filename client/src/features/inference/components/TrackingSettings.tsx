// TrackingSettings.tsx — panel de Seguimiento (columna derecha de Inferencia).
//
// Por que es una SECCION APARTE de "Render" y no tres interruptores mas ahi adentro:
// Render gobierna como se PINTA una deteccion (estilo, etiquetas, sombreado, grosor)
// y sus controles son independientes entre si y valen para cualquier fuente. Estos
// tres son otra cosa: gobiernan como se sigue un objeto A LO LARGO DEL TIEMPO, tienen
// dependencias entre ellos, y solo tienen efecto con camara o video. Agrupados, esas
// dos reglas se aplican al bloque entero en vez de repetirse control por control.
//
// Los tres son ajustes del USUARIO (persisten en localStorage y viajan por el mismo
// POST /config/draw), pero habilitan MEMORIA en el backend que vive por conexion del
// WebSocket. El cliente no guarda esa memoria: solo dice si la quiere.

import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';
import type { DrawSettings } from '@/features/vision-workspace/services/types';
import { Interruptor } from '@/shared/ui/Interruptor';
import { pushDrawSettings } from '../api/drawSettings';
import { useStreamStore } from '../store/streamStore';

/**
 * Aplica las dependencias entre los tres ajustes, igual que update_draw_config() en
 * el backend (render/draw_config.py):
 *
 *   - pedir suavizado, trazas o el acumulado de zona PRENDE el seguimiento
 *   - apagar el seguimiento APAGA los tres
 *
 * No es cosmetica. Sin `tracker_id` el suavizado de supervision no suaviza y avisa
 * (el toggle quedaria prendido sin hacer nada, que es exactamente lo que hay que
 * evitar) y el annotator de trazas directamente levanta ValueError y rompe el frame.
 *
 * La regla esta duplicada a proposito: el backend es la autoridad y ya la fuerza en
 * su unica puerta de escritura, pero aplicarla tambien aca evita que el panel
 * parpadee mientras espera la respuesta. Ambos convergen porque es la misma regla.
 */
export function aplicarDependencias(patch: Partial<DrawSettings>): Partial<DrawSettings> {
  const resultado = { ...patch };
  if (patch.tracking === false) {
    resultado.smoothing = false;
    resultado.traces = false;
    // El acumulado de zona vive en el panel de Zonas, pero cuelga del mismo maestro:
    // cuenta objetos DISTINTOS, y sin tracker_id no hay objetos distintos que contar.
    resultado.zoneTotal = false;
  }
  if (patch.smoothing || patch.traces || patch.zoneTotal) {
    resultado.tracking = true;
  }
  return resultado;
}

/**
 * Si el panel tiene algo que gobernar para la fuente activa.
 *
 * Con una imagen fija NO: el cliente abre un WebSocket efimero declarado
 * `?stateful=false` y el backend no arma memoria para una foto suelta. Una secuencia
 * de un frame no tiene nada que rastrear. Sin fuente (`none`) se deja operable a
 * proposito, igual que el panel de Render sin modelo: son ajustes del usuario y
 * dejarlos listos antes de empezar es legitimo.
 */
export function seguimientoAplicaA(sourceKind: string): boolean {
  return sourceKind !== 'file-image';
}

export function TrackingSettings() {
  const drawSettings = useWorkspaceStore((s) => s.drawSettings);
  const setDrawSettings = useWorkspaceStore((s) => s.setDrawSettings);
  const sourceKind = useStreamStore((s) => s.source.kind);

  const operable = seguimientoAplicaA(sourceKind);

  // No se llama a resendStill(): estos ajustes solo valen para camara y video, que
  // refrescan solos. La unica fuente que necesitaria un re-envio es la imagen fija, y
  // ahi el panel esta deshabilitado justamente porque no cambian nada.
  function aplicar(patch: Partial<DrawSettings>) {
    const efectivo = aplicarDependencias(patch);
    setDrawSettings(efectivo);
    pushDrawSettings({ ...drawSettings, ...efectivo });
  }

  const razonApagado = operable
    ? null
    : 'Solo aplica a camara y video: una imagen fija es un frame suelto, no una secuencia.';

  return (
    <div className="flex flex-col gap-2">
      <Interruptor
        label="Seguimiento"
        hint={
          razonApagado ??
          'Le da a cada objeto una identidad estable entre frames y la muestra como #id en la etiqueta. Sirve para ver si el modelo pierde el objeto o lo detecta siempre.'
        }
        on={drawSettings.tracking}
        disabled={!operable}
        onToggle={() => aplicar({ tracking: !drawSettings.tracking })}
      />

      <Interruptor
        dependiente
        label={`Suavizado (n=${drawSettings.smoothingLength})`}
        hint={
          razonApagado ??
          'Promedia la posicion de cada objeto en los ultimos frames: la caja deja de temblar, pero queda unos pixeles POR DETRAS del objeto en movimiento. Estas viendo un promedio, no la salida cruda del modelo. Requiere seguimiento.'
        }
        on={drawSettings.smoothing}
        disabled={!operable}
        onToggle={() => aplicar({ smoothing: !drawSettings.smoothing })}
      />

      <Interruptor
        dependiente
        label="Trazas"
        hint={
          razonApagado ??
          'Dibuja la estela del recorrido de cada objeto. Una traza que salta de un objeto a otro delata que el seguimiento confunde identidades; una entrecortada, que el detector pierde el objeto. Requiere seguimiento.'
        }
        on={drawSettings.traces}
        disabled={!operable}
        onToggle={() => aplicar({ traces: !drawSettings.traces })}
      />

      {!operable && (
        <p className="px-0.5 text-[11px] leading-snug text-label">
          Solo con camara o video: una imagen fija es un frame suelto.
        </p>
      )}
    </div>
  );
}
