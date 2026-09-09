// Recorder.tsx — la barra de transporte: grabar el VIDEO de salida y volcar las
// DETECCIONES. Son dos botones hermanos porque son la misma idea aplicada a las dos
// mitades de lo que el sistema produce — la imagen compuesta y los numeros que la
// generaron — y porque muchas veces se quieren las dos del mismo tramo.
//
// Son independientes a proposito: se puede exportar sin grabar (una sesion larga no
// necesita arrastrar un video de cientos de MB) y grabar sin exportar.
//
// La logica no vive aca: el video en useRecorder, las detecciones en
// useDetectionExport.

import { type RefObject } from 'react';
import { cn } from '@/shared/ui/cn';
import { Badge } from '@/shared/ui/Badge';
import { useRecorder } from '../hooks/useRecorder';
import { useDetectionExport } from '../hooks/useDetectionExport';
import { useStreamStore } from '../store/streamStore';

export function Recorder({ canvasRef }: { canvasRef: RefObject<HTMLCanvasElement | null> }) {
  const { recording, error, formato, start, stop } = useRecorder(canvasRef);
  const exportacion = useDetectionExport();
  const sourceKind = useStreamStore((s) => s.source.kind);
  // Igual que grabar video: sobre una foto suelta no hay nada que registrar en el
  // tiempo, y la conexion one-shot se cierra antes de que se pueda parar el volcado.
  const hayStream = sourceKind === 'camera' || sourceKind === 'file-video';

  return (
    <div className="flex items-center gap-5">
      {/* ── Video ── */}
      <div className="flex items-center gap-3">
        <BotonRedondo
          activo={recording}
          disabled={!hayStream}
          onClick={recording ? stop : start}
          label={recording ? 'Detener grabacion' : 'Iniciar grabacion'}
          title={
            hayStream
              ? `Graba el feed compuesto a .${formato ?? '?'}`
              : 'Solo con camara o video'
          }
        >
          <span
            className={cn(
              'size-3 bg-danger transition-all duration-150',
              recording ? 'rounded-[2px]' : 'rounded-full',
            )}
          />
        </BotonRedondo>

        {recording ? (
          <Badge variant="rec">REC</Badge>
        ) : (
          <span className="text-[11px] text-fg-subtle">
            Grabar salida
            {/* Se muestra el formato porque se ELIGE segun lo que el runtime soporte
                (mp4 si puede, webm si no): decir cual salio evita la sorpresa. */}
            {formato && <span className="ml-1 font-mono text-[10px] text-label">.{formato}</span>}
          </span>
        )}
      </div>

      <span aria-hidden className="h-6 w-px bg-border" />

      {/* ── Detecciones ── */}
      <div className="flex items-center gap-3">
        <BotonRedondo
          activo={exportacion.exportando}
          disabled={!hayStream}
          onClick={exportacion.exportando ? exportacion.stop : exportacion.start}
          label={exportacion.exportando ? 'Terminar el volcado' : 'Guardar detecciones'}
          title={
            hayStream
              ? 'Guarda cada deteccion de cada frame en un .json. Con el seguimiento prendido incluye el #id, que es lo que permite reconstruir el recorrido de cada objeto.'
              : 'Solo con camara o video'
          }
        >
          {/* Llaves de JSON: dice que sale un archivo de datos, no una imagen. */}
          <svg viewBox="0 0 16 16" aria-hidden className="size-4">
            <path
              d="M6.2 2.5c-1.5 0-1.9.7-1.9 1.9v1.7c0 1-.5 1.5-1.3 1.9.8.4 1.3.9 1.3 1.9v1.7c0 1.2.4 1.9 1.9 1.9M9.8 2.5c1.5 0 1.9.7 1.9 1.9v1.7c0 1 .5 1.5 1.3 1.9-.8.4-1.3.9-1.3 1.9v1.7c0 1.2-.4 1.9-1.9 1.9"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              className={exportacion.exportando ? 'text-accent' : 'text-fg-subtle'}
            />
          </svg>
        </BotonRedondo>

        {exportacion.exportando ? (
          <Badge variant="rec">DATOS</Badge>
        ) : (
          <span className="text-[11px] text-fg-subtle">
            Guardar detecciones
            {/* Cuantas filas salieron: sin esto, un volcado sin detecciones y uno
                exitoso se ven exactamente igual (en los dos casos "no pasa nada"). */}
            {exportacion.ultimasFilas != null && (
              <span className="ml-1 font-mono text-[10px] text-label">
                {exportacion.ultimasFilas} filas
              </span>
            )}
          </span>
        )}
      </div>

      {(error || exportacion.error) && (
        <p className="text-xs text-danger">{error ?? exportacion.error}</p>
      )}
    </div>
  );
}

// Boton de transporte: circulo con contorno, que se marca cuando esta activo.
function BotonRedondo({
  activo,
  disabled,
  onClick,
  label,
  title,
  children,
}: {
  activo: boolean;
  disabled?: boolean;
  onClick: () => void;
  label: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={activo}
      title={title}
      className={cn(
        'grid size-9 shrink-0 place-items-center rounded-full border bg-control',
        'transition-colors duration-150 focus-visible:outline-none active:scale-95',
        'disabled:cursor-not-allowed disabled:opacity-40',
        activo ? 'border-[rgba(255,77,79,0.35)]' : 'border-border hover:border-border-strong',
      )}
    >
      {children}
    </button>
  );
}
