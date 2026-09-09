// VisionWorkspace.tsx — superficie de presentacion (presentacional).
// Monta el <video> oculto (fuente), el canvas de salida y el overlayRoot (capa HTML).
// La orquestacion del stream vive en useVisionSession (feature inference), que recibe
// estos refs. children = slots superpuestos (ej. MetricsHUD).

import { type ReactNode, type RefObject } from 'react';
import { useWorkspaceStore } from '../store/workspaceStore';
import { getStrategy } from '../services/registry';
import { UnsupportedOverlay } from './UnsupportedOverlay';
import { ModelLoadingOverlay } from './ModelLoadingOverlay';
import { ZoneEditor } from './ZoneEditor';

interface VisionWorkspaceProps {
  videoRef: RefObject<HTMLVideoElement | null>;
  canvasRef: RefObject<HTMLCanvasElement | null>;
  overlayRef: RefObject<HTMLDivElement | null>;
  hasSource: boolean; // hay una fuente activa (camara/archivo)
  children?: ReactNode; // overlays (HUD)
}

export function VisionWorkspace({
  videoRef,
  canvasRef,
  overlayRef,
  hasSource,
  children,
}: VisionWorkspaceProps) {
  const activeModel = useWorkspaceStore((s) => s.activeModel);
  const loadingModel = useWorkspaceStore((s) => s.loadingModel);
  const unsupported = activeModel ? !getStrategy(activeModel.type).implemented : false;

  return (
    <div
      className="relative grid h-full place-items-center overflow-hidden rounded-[var(--radius-lg)] border border-border bg-feed"
      // Grilla sutil de instrumento (cian al 5%) en las bandas de letterbox.
      style={{
        backgroundImage:
          'linear-gradient(rgba(52,214,255,.05) 1px, transparent 1px), linear-gradient(90deg, rgba(52,214,255,.05) 1px, transparent 1px)',
        backgroundSize: '30px 30px',
      }}
    >
      {/* Fuente: oculta, solo alimenta al stream. */}
      <video ref={videoRef} className="hidden" playsInline muted />

      {/* Salida: el frame es el heroe; object-contain conserva el aspecto. */}
      <canvas
        ref={canvasRef}
        className="max-h-full max-w-full object-contain"
      />

      {/* Capa HTML de overlays (badges de clasificacion, leyendas). */}
      <div ref={overlayRef} className="pointer-events-none absolute inset-0" />

      {/* Editor de zonas. Solo captura el cursor mientras se esta editando; el resto
          del tiempo es un div inerte que existe para poder medir la caja del canvas.
          La zona CONFIRMADA no se dibuja aca: la pinta el backend dentro del frame. */}
      <ZoneEditor canvasRef={canvasRef} />

      {/* Estado vacio. */}
      {!hasSource && (
        <div className="absolute inset-0 grid place-items-center">
          <p className="text-sm text-fg-subtle">No hay fuente de video seleccionada</p>
        </div>
      )}

      {unsupported && activeModel && <UnsupportedOverlay type={activeModel.type} />}

      {children}

      {/* Ultimo en el arbol y con z-10: mientras se arma un modelo tapa TODO lo de
          arriba (frame, HUD, badges). Es el estado dominante de la pantalla. */}
      {loadingModel && <ModelLoadingOverlay name={loadingModel} />}
    </div>
  );
}
