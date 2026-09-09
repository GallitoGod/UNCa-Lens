// InferenceView.tsx — vista de inferencia con la anatomia de 3 zonas del spec:
//   izquierda  -> ENTRADA y salud del modelo: fuente, modelos, errores
//   centro     -> feed (heroe) + barra de transporte
//   derecha    -> PRESENTACION y medicion: parametros, render, seguimiento, metricas
// Es duena de los refs (video/canvas/overlay) y del orquestador (useVisionSession).
//
// Todas las secciones son PLEGABLES (shared/ui/Section). El motivo es de crecimiento:
// cada cosecha de supervision le suma una seccion a la columna derecha y no da mas.
// Los errores se mudaron a la IZQUIERDA porque son del MODELO —los produce el modelo
// cargado, y /logs/inference devuelve errores de inferencia—, y porque la columna que
// crece es la derecha: la izquierda (fuente y modelo) es estable, asi que ahi hay aire.

import { useRef, useState } from 'react';
import { VisionWorkspace } from '@/features/vision-workspace/components/VisionWorkspace';
import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';
import { Tabs } from '@/shared/ui/Tabs';
import { Badge } from '@/shared/ui/Badge';
import { Section } from '@/shared/ui/Section';
import { useStreamStore } from './store/streamStore';
import { useVisionSession } from './hooks/useVisionSession';
import { CameraSource } from './components/CameraSource';
import { FileSource } from './components/FileSource';
import { ModelSelector } from './components/ModelSelector';
import { ConfidenceSlider } from './components/ConfidenceSlider';
import { RenderSettings, panelDeRenderAplica } from './components/RenderSettings';
import { TrackingSettings } from './components/TrackingSettings';
import { ZoneSettings } from './components/ZoneSettings';
import { MetricsHUD } from './components/MetricsHUD';
import { MetricsPanel } from './components/MetricsPanel';
import { LogPanel } from './components/LogPanel';
import { Recorder } from './components/Recorder';
import { useInferenceLogs } from './hooks/useDiagnostics';
import { ESTILOS } from './components/RenderSettings';

type SourceTab = 'camera' | 'file';

export default function InferenceView() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const [sourceTab, setSourceTab] = useState<SourceTab>('camera');

  const sourceKind = useStreamStore((s) => s.source.kind);
  const hasSource = sourceKind !== 'none';
  const isLive = sourceKind === 'camera';

  // El panel de render gobierna COMO se dibuja el resultado sobre el frame, cosa que
  // solo existe para los tipos que el backend compone. Con un clasificador cargado la
  // columna lo esconde en vez de dejar perillas que no mueven nada.
  const activeType = useWorkspaceStore((s) => s.activeModel?.type ?? null);
  const activeModelName = useWorkspaceStore((s) => s.activeModel?.name ?? null);
  const mostrarRender = panelDeRenderAplica(activeType);

  // Resumenes para leer cada seccion SIN desplegarla (ver shared/ui/Section).
  const drawSettings = useWorkspaceStore((s) => s.drawSettings);
  const estiloActivo = ESTILOS.find((e) => e.key === drawSettings.boxStyle)?.label ?? '';
  const seguimientoOn = drawSettings.tracking;
  const cantidadZonas = useStreamStore((s) => s.zonas.length);
  // Ahora se puede: el umbral dejo de ser un useState del slider y vive en el store.
  // Era lo unico que faltaba del pendiente #25 para que Parametros tuviera estado, y
  // el motivo por el que se resolvio no fue estetico — el numero que mostraba el
  // slider no era el que el sistema usaba.
  const confianza = useWorkspaceStore((s) => s.confidence);

  // Los errores se consultan SIEMPRE, este la seccion abierta o no: un contador de
  // errores que deja de contar al plegarse es peor que no tener contador. Las
  // metricas, en cambio, solo se piden con la seccion abierta — plegarlas no oculta
  // nada que se este perdiendo. Comparten queryKey con sus paneles, asi que TanStack
  // deduplica y esto no agrega una segunda ronda de polling.
  const { data: errores } = useInferenceLogs(true);
  const cantidadErrores = errores?.length ?? 0;

  // Orquesta media + stream + render segun la fuente activa.
  useVisionSession({ videoRef, canvasRef, overlayRef });

  return (
    <div className="grid h-full grid-cols-[200px_1fr_230px] gap-3 bg-canvas p-3">
      {/* ── Zona izquierda: fuente + modelos ── */}
      <aside className="flex flex-col gap-5 overflow-y-auto rounded-[var(--radius-lg)] border border-border bg-surface p-4">
        <Section id="fuente" title="Fuente" estado={sourceTab === 'camera' ? 'CAMARA' : 'ARCHIVO'}>
          <Tabs
            aria-label="Fuente de video"
            tabs={[
              { key: 'camera', label: 'Camara' },
              { key: 'file', label: 'Archivo' },
            ]}
            value={sourceTab}
            onChange={setSourceTab}
          />
          {sourceTab === 'camera' ? <CameraSource /> : <FileSource />}
        </Section>

        <Section id="modelo" title="Modelo" estado={activeModelName ?? 'NINGUNO'} className="min-h-0">
          <ModelSelector />
        </Section>

        {/* Los errores viven ACA, con el modelo que los produce. Su encabezado cuenta
            aunque este plegado: es el unico estado que no puede quedar mudo. */}
        <Section
          id="errores"
          title="Errores"
          estado={cantidadErrores > 0 ? String(cantidadErrores) : 'OK'}
          alerta={cantidadErrores > 0}
        >
          <LogPanel />
        </Section>
      </aside>

      {/* ── Zona central: feed (heroe) + transporte ── */}
      <div className="flex min-w-0 flex-col gap-3">
        <VisionWorkspace
          videoRef={videoRef}
          canvasRef={canvasRef}
          overlayRef={overlayRef}
          hasSource={hasSource}
        >
          <MetricsHUD open />
          {isLive && (
            <div className="absolute right-3 top-3">
              <Badge variant="live">En vivo</Badge>
            </div>
          )}
        </VisionWorkspace>

        {/* Barra de transporte */}
        <div className="flex items-center gap-4 rounded-[var(--radius-lg)] border border-border bg-surface px-4 py-3">
          <Recorder canvasRef={canvasRef} />
        </div>
      </div>

      {/* ── Zona derecha: parametros + metricas + errores ── */}
      <aside className="flex flex-col gap-5 overflow-y-auto rounded-[var(--radius-lg)] border border-border bg-surface p-4">
        <Section
          id="parametros"
          title="Parametros"
          estado={confianza === null ? undefined : `${Math.round(confianza * 100)}%`}
        >
          <ConfidenceSlider />
        </Section>

        {/* La seccion ENTERA se va cuando no aplica, no solo los controles: un
            encabezado huerfano sobre un hueco se lee como un panel roto. Esto es
            RELEVANCIA (la decide el sistema) y es independiente del plegado (lo decide
            el usuario): la primera define que existe, el segundo que esta desplegado. */}
        {mostrarRender && (
          <Section id="render" title="Render" estado={estiloActivo}>
            <RenderSettings />
          </Section>
        )}

        {/* Seguimiento cuelga de la MISMA condicion que Render, y por el mismo motivo:
            necesita que el backend componga el frame (output_kind="frame"). Con un
            clasificador no hay geometria que rastrear ni sobre que dibujar una estela.
            Va como seccion aparte de Render porque gobierna el eje del TIEMPO, no el
            del pintado, y porque solo aplica a camara y video. */}
        {mostrarRender && (
          <Section id="seguimiento" title="Seguimiento" estado={seguimientoOn ? 'ON' : undefined}>
            <TrackingSettings />
          </Section>
        )}

        {/* Zonas cuelga de la MISMA condicion que Render y Seguimiento: con un
            clasificador el backend no compone frame, asi que no hay nada sobre lo que
            dibujar un poligono ni detecciones con geometria que contar. */}
        {mostrarRender && (
          <Section
            id="zonas"
            title="Zonas"
            estado={cantidadZonas > 0 ? String(cantidadZonas) : undefined}
          >
            <ZoneSettings />
          </Section>
        )}

        {/* SIN 'estado' a proposito, y no por olvido: las metricas se dejan de pedir al
            plegarse, asi que el encabezado solo podria mostrar el ultimo valor cacheado
            — un fps congelado que se lee como si fuera en vivo. Eso es peor que no
            mostrar nada, y es exactamente la clase de estado enganoso que la regla del
            encabezado existe para evitar. El fps en vivo ya lo da el MetricsHUD, que
            flota sobre el feed y no depende de esta seccion. */}
        <Section id="metricas" title="Metricas">
          <MetricsPanel />
        </Section>
      </aside>
    </div>
  );
}
