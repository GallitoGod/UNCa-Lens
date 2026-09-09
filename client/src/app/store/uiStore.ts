// uiStore.ts — estado de UI de primer nivel: vista activa y secciones plegadas.
// La navegacion es por estado (sin URLs; la app no las usa). El tema es unico
// (dark-only, piel Cabina Tecnica), asi que ya no hay estado de tema ni toggle.

import { create } from 'zustand';

export type View = 'inference' | 'models';

/** Identificadores de las secciones plegables de Inferencia. */
export type SectionId =
  | 'fuente'
  | 'modelo'
  | 'errores'
  | 'parametros'
  | 'render'
  | 'etiquetas'
  | 'seguimiento'
  | 'zonas'
  | 'metricas';

// Que nace abierto y que no, elegido por CUANTO SE TOCA cada cosa, no por orden:
//   - fuente/modelo: es por donde se empieza.
//   - parametros: el umbral es el control mas tocado de la app.
//   - metricas: es un instrumento, se mira de reojo mientras se hace otra cosa.
//   - render/seguimiento: configurar-y-olvidar; se abren cuando se los busca.
//   - zonas: cerrada. Dibujar una zona es una decision, no algo que se toque seguido,
//     y su encabezado plegado dice cuantas hay.
//   - etiquetas: ANIDADA dentro de render, y por eso nace ABIERTA: ya esta escondida
//     detras del plegado del padre, y dejarla cerrada obligaria a dos clicks para
//     llegar a un control que se toca seguido con modelos de muchas detecciones.
//   - errores: cerrada, pero su encabezado SIEMPRE muestra el contador (ver Section).
const SECCIONES_POR_DEFECTO: Record<SectionId, boolean> = {
  fuente: true,
  modelo: true,
  errores: false,
  parametros: true,
  render: false,
  etiquetas: true,
  seguimiento: false,
  zonas: false,
  metricas: true,
};

interface UiState {
  activeView: View;
  setView: (view: View) => void;
  /** Que secciones estan desplegadas. Persistido: es una preferencia del usuario. */
  sections: Record<SectionId, boolean>;
  toggleSection: (id: SectionId) => void;
}

// Misma persistencia manual que workspaceStore (sin middleware, por coherencia).
const SECCIONES_KEY = 'uncalens-sections';

function leerSecciones(): Record<SectionId, boolean> {
  try {
    const raw = localStorage.getItem(SECCIONES_KEY);
    if (!raw) return SECCIONES_POR_DEFECTO;
    // Merge sobre los defaults: tolera versiones viejas sin las claves nuevas, que
    // es exactamente lo que va a pasar cada vez que se agregue una seccion.
    return { ...SECCIONES_POR_DEFECTO, ...(JSON.parse(raw) as Partial<Record<SectionId, boolean>>) };
  } catch {
    return SECCIONES_POR_DEFECTO;
  }
}

export const useUiStore = create<UiState>((set) => ({
  activeView: 'inference',
  setView: (view) => set({ activeView: view }),

  sections: leerSecciones(),
  toggleSection: (id) =>
    set((s) => {
      const next = { ...s.sections, [id]: !s.sections[id] };
      try {
        localStorage.setItem(SECCIONES_KEY, JSON.stringify(next));
      } catch {
        // localStorage lleno/deshabilitado: el cambio sigue valiendo en memoria.
      }
      return { sections: next };
    }),
}));
