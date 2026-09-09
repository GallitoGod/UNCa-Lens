// ModelSelector.tsx — lista de modelos cargables del panel izquierdo de inferencia.
// Selecciona el modelo a cargar en el backend y fija el modelo activo del workspace
// (cuyo type enruta la estrategia de presentacion). Misma logica que antes; ahora se
// presenta como lista de ModelRow en vez de un <select>.

import { useEffect, useRef } from 'react';
import type { ModelType } from '@/shared/api/types';
import { useModels, useSelectModel } from '../hooks/useModels';
import { getModelType } from '../api/models';
import { pushDrawSettings } from '../api/drawSettings';
import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';
import { useStreamStore } from '../store/streamStore';
import { ModelRow } from './ModelRow';

export function ModelSelector() {
  const { data: models, isLoading } = useModels();
  const selectModel = useSelectModel();
  const activeModel = useWorkspaceStore((s) => s.activeModel);
  const setActiveModel = useWorkspaceStore((s) => s.setActiveModel);
  const resendStill = useStreamStore((s) => s.resendStill);
  const autoSelected = useRef(false);

  // Modelo que se esta cargando ahora mismo. Vive en el workspaceStore y no en la
  // mutation porque el cartel "armando hot path" que tapa el feed tambien lo
  // necesita, y la carga no termina cuando responde /select_model: falta leer el
  // model_type del config. Una sola fuente de verdad para fila + overlay.
  const loadingName = useWorkspaceStore((s) => s.loadingModel);
  const setLoadingModel = useWorkspaceStore((s) => s.setLoadingModel);

  async function handleSelect(name: string) {
    if (!name) return;
    if (useWorkspaceStore.getState().loadingModel) return; // una carga por vez
    setLoadingModel(name);
    try {
      // El umbral EFECTIVO del modelo recien cargado. Se adopta en vez de imponerle
      // el que hubiera en pantalla: cada config trae el suyo calibrado (best usa 0.15
      // por ser vista aerea con objetos chicos) y pisarlo con un valor generico dejaria
      // al modelo casi sin detecciones.
      const umbral = await selectModel.mutateAsync(name);
      if (umbral !== null) useWorkspaceStore.getState().setConfidence(umbral);
      // Leer el model_type real del config (GET /configs/{name}) para enrutar la
      // estrategia del workspace. Si no se puede leer, se asume 'detection' (es el
      // unico tipo cargable hoy; CLS/SEG aun dan 501 al cargar en el backend).
      let type: ModelType = 'detection';
      try {
        const real = await getModelType(name);
        if (real) type = real;
      } catch (e) {
        console.warn('No se pudo leer el model_type del config, se asume detection:', e);
      }
      setActiveModel(name, type);
      // Re-empujar los colores: el backend puede haberse reiniciado (vuelve a sus
      // defaults) mientras el cliente seguia vivo con los del usuario.
      pushDrawSettings(useWorkspaceStore.getState().drawSettings);
      // Fuente estatica: volver a inferir el frame actual con el modelo nuevo. Sin
      // esto la pantalla queda mostrando el resultado del modelo anterior.
      resendStill();
    } catch (err) {
      console.error('No se pudo seleccionar el modelo:', err);
    } finally {
      // En finally: si la carga falla (404/422/501) el cartel TIENE que irse igual,
      // si no la pantalla queda tapada para siempre.
      setLoadingModel(null);
    }
  }

  // Auto-seleccionar el primero al cargar (una sola vez).
  useEffect(() => {
    if (!autoSelected.current && models && models.length > 0 && !activeModel) {
      autoSelected.current = true;
      void handleSelect(models[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);

  if (isLoading) {
    return <p className="px-1 font-mono text-xs text-fg-subtle">Cargando modelos...</p>;
  }
  if (!models?.length) {
    return <p className="px-1 font-mono text-xs text-fg-subtle">Sin modelos disponibles</p>;
  }

  return (
    <div className="flex flex-col gap-1.5">
      {models.map((m) => (
        <ModelRow
          key={m}
          name={m}
          active={activeModel?.name === m}
          loading={loadingName === m}
          // Mientras carga uno, el resto queda inerte: evita encolar cargas y deja
          // claro que la app esta ocupada, no colgada.
          disabled={loadingName !== null && loadingName !== m}
          onSelect={() => void handleSelect(m)}
        />
      ))}
    </div>
  );
}
