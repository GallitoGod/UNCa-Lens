// workspaceStore.ts — estado del vision-workspace: modelo activo (name + type) y
// los ajustes de dibujo. El type del modelo activo es lo que usa el render para
// enrutar a la estrategia correcta.

import { create } from 'zustand';
import type { ModelType } from '@/shared/api/types';
import type { DrawSettings } from '../services/types';

interface WorkspaceState {
  activeModel: { name: string; type: ModelType } | null;
  /**
   * Modelo que se esta armando en el backend ahora mismo (null = ninguno). Cargar
   * un modelo no es instantaneo -sesion del runtime + warmup- y durante ese rato el
   * canvas mostraba el frame viejo como si nada, dando la sensacion de que la app se
   * colgo. El workspace lo lee para tapar el feed con el cartel de carga.
   */
  loadingModel: string | null;
  /**
   * Umbral de confianza vigente en el BACKEND, en [0,1]. Null = todavia no se sabe
   * (sin modelo cargado).
   *
   * Vive en el store y no como useState del slider, y eso arregla un bug real: el
   * slider arrancaba en un 50% hardcodeado que nunca se enviaba ni se leia, asi que el
   * panel decia 50% mientras el backend filtraba con lo que declaraba el config del
   * modelo (0.15 en best). Mas de la mitad de las detecciones dibujadas y exportadas
   * estaban por debajo del numero que el panel afirmaba — exactamente la clase de
   * control cuyo estado visible no es el del sistema que este proyecto evita.
   *
   * NO se persiste: pertenece al modelo cargado, no al usuario. Cargar un modelo lo
   * reemplaza por el suyo, que es lo correcto — cada config esta calibrado aparte y
   * forzarle a 'best' el umbral de otro lo dejaria casi sin detecciones.
   */
  confidence: number | null;
  drawSettings: DrawSettings;
  setActiveModel: (name: string, type: ModelType) => void;
  clearActiveModel: () => void;
  setLoadingModel: (name: string | null) => void;
  setConfidence: (value: number | null) => void;
  setDrawSettings: (patch: Partial<DrawSettings>) => void;
}

// Default de bbox cian (coherente con el #00BFFF historico); label oscuro legible
// sobre el fondo cian de la etiqueta.
const DEFAULT_DRAW_SETTINGS: DrawSettings = {
  bboxColor: '#00BFFF',
  labelColor: '#001018',
  maskAlpha: 0.5,
  // Los defaults coinciden con los del backend (render/draw_config.py).
  // smartLabels y autoScale nacen prendidos: son mejores defaults, y el panel de
  // render los deja apagar. shading nace APAGADO: con muchas cajas superpuestas los
  // rellenos se suman y tapan la imagen, asi que lo prende quien lo quiera mirar.
  //
  // Los tres de seguimiento nacen APAGADOS y no por costo (~1,3 ms/frame los tres):
  // rastrear es una herramienta de inspeccion que se prende cuando se quiere
  // responder algo concreto, y el suavizado ademas MAQUILLA al modelo (promedia la
  // salida cruda), que es justo lo que un banco de pruebas no deberia hacer solo.
  boxStyle: 'box',
  // 'completa' es lo que el sistema venia haciendo y sigue siendo lo correcto con
  // pocas cajas; los otros dos modos existen para el caso contrario (ver #27).
  labelMode: 'completa',
  smartLabels: true,
  shading: false,
  autoScale: true,
  tracking: false,
  smoothing: false,
  smoothingLength: 5,
  traces: false,
  tracesLength: 30,
  // Ambar y no el cian de las cajas: una zona del mismo color que las detecciones se
  // confunde con ellas justo cuando hay muchas, que es cuando la zona sirve.
  zoneColor: '#FFB020',
  // 'centro' y no el borde inferior (que es el default de supervision): es el unico
  // anclaje que se comporta igual en vista aerea y en vista de calle, y el primer
  // modelo propio del usuario ('best', VisDrone) es de dron.
  zoneAnchor: 'centro',
  // Apagado: el acumulado solo tiene sentido sobre una secuencia y arrastra el costo
  // del tracking (~0,54 ms/frame) para algo que no todos quieren mirar.
  zoneTotal: false,
};

// Persistencia en localStorage (mismo patron manual que uiStore, sin middleware).
// SDD 4.1.3: los colores de dibujo deben sobrevivir entre sesiones.
const DRAW_KEY = 'uncalens-draw-settings';

function readStoredDrawSettings(): DrawSettings {
  try {
    const raw = localStorage.getItem(DRAW_KEY);
    if (!raw) return DEFAULT_DRAW_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<DrawSettings>;
    // Merge sobre los defaults: tolera versiones viejas sin claves nuevas
    // (ej: maskAlpha/colormap agregados despues).
    return { ...DEFAULT_DRAW_SETTINGS, ...parsed };
  } catch {
    return DEFAULT_DRAW_SETTINGS;
  }
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  activeModel: null,
  loadingModel: null,
  confidence: null,
  drawSettings: readStoredDrawSettings(),

  setActiveModel: (name, type) => set({ activeModel: { name, type } }),
  clearActiveModel: () => set({ activeModel: null }),
  setLoadingModel: (loadingModel) => set({ loadingModel }),
  setConfidence: (confidence) => set({ confidence }),
  setDrawSettings: (patch) =>
    set((s) => {
      const next = { ...s.drawSettings, ...patch };
      try {
        localStorage.setItem(DRAW_KEY, JSON.stringify(next));
      } catch {
        // localStorage lleno/deshabilitado: el cambio sigue valiendo en memoria.
      }
      return { drawSettings: next };
    }),
}));
