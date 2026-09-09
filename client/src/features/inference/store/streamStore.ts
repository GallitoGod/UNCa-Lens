// streamStore.ts — fuente de video activa + estado del stream. Los componentes de
// fuente (CameraSource/FileSource) escriben la fuente; el orquestador
// (useVisionSession) reacciona y maneja el media + el WS.

import { create } from 'zustand';
import type { StreamStatus } from '../services/videoStream';
import type { Zona } from '../services/geometry';

/**
 * En que esta el editor de zonas.
 *   off       -> el cliente no dibuja nada: la zona confirmada la pinta el BACKEND.
 *   dibujando -> se esta armando un poligono nuevo, clic a clic.
 *   editando  -> se muestran los vertices arrastrables de las zonas ya confirmadas.
 *
 * Que 'off' no dibuje nada es la regla, no una optimizacion: el cliente dejo de
 * dibujar resultados del modelo en el paso 3 (2026-08-26). Mientras se ARMA el
 * poligono si lo pinta el cliente, y eso no viola la regla porque lo que dibuja es un
 * CONTROL, no un resultado — no se puede ir y volver al backend a la velocidad del
 * arrastre. Al confirmar, el dibujo pasa al backend.
 */
export type ModoZona = 'off' | 'dibujando' | 'editando';

export type Source =
  | { kind: 'none' }
  | { kind: 'camera'; deviceId: string }
  | { kind: 'file-video'; url: string }
  | { kind: 'file-image'; url: string };

interface StreamState {
  source: Source;
  status: StreamStatus;
  lastError: string | null;
  /**
   * Contador de "volve a inferir el frame actual". Solo aplica a fuentes ESTATICAS
   * (imagen): con camara o video ya llega un frame nuevo cada tick y el cambio se ve
   * solo. Con una imagen el envio es one-shot, asi que cambiar de modelo o mover el
   * umbral no producia ninguna inferencia nueva y la pantalla quedaba mostrando el
   * resultado viejo. Quien cambia un parametro llama a resendStill().
   */
  stillNonce: number;
  /**
   * Zonas poligonales de la escena, en coordenadas normalizadas [0,1].
   *
   * Viven ACA y no en workspaceStore (donde viven los ajustes de dibujo) porque
   * describen LA ESCENA, no al usuario: mueren cuando cambia la fuente y no se
   * persisten. Que el store dueno de la fuente sea tambien el dueno del poligono
   * hace que el borrado sea ESTRUCTURAL —cambiar de fuente las limpia— en vez de
   * depender de que alguien se acuerde de llamar a algo. Es el mismo criterio con
   * el que el backend le dio la memoria de tracking a la conexion del WebSocket.
   *
   * El COLOR de la zona y el ANCLAJE no estan aca: esos si son del usuario y
   * persisten (drawSettings).
   */
  zonas: Zona[];
  /** Sobre que tamano de frame se dibujaron (para el guard de aspecto del backend). */
  zonasFrame: { w: number; h: number } | null;
  setZonas: (zonas: Zona[], frame?: { w: number; h: number } | null) => void;
  modoZona: ModoZona;
  setModoZona: (modo: ModoZona) => void;
  /**
   * Pedido de "pone la cuenta en cero", con el id de la zona (null = todas).
   *
   * Va por nonce y no por llamada directa porque quien aprieta el boton (el panel) no
   * tiene el WebSocket: lo tiene useVisionSession. Es el mismo patron que stillNonce,
   * por la misma razon, y evita exponer el handle del stream en un store.
   */
  zonaReset: { id: string | null; nonce: number };
  resetZona: (id?: string | null) => void;
  /**
   * Estado del volcado de detecciones de la conexion viva.
   *
   * `activa` la escribe el ACK del backend, no el click: el backend es quien abre y
   * cierra el archivo, asi que es el unico que sabe de verdad si hay un volcado en
   * curso. Si el cliente lo marcara al apretar el boton, un fallo de I/O dejaria el
   * boton en 'grabando' sin que se este grabando nada — el sintoma que este proyecto
   * viene evitando en todas sus superficies.
   */
  exportacion: { activa: boolean; archivo: string | null; filas: number; error: string | null };
  /** Pedido al backend (lo despacha useVisionSession, que tiene el WebSocket). */
  pedirExport: (que: 'start' | 'stop') => void;
  exportNonce: { que: 'start' | 'stop'; nonce: number };
  /** Lo llama useVisionSession al llegar el ack. */
  setExportacion: (e: { activa: boolean; archivo: string | null; filas: number; error: string | null }) => void;
  setCameraSource: (deviceId: string) => void;
  setFileVideo: (url: string) => void;
  setFileImage: (url: string) => void;
  clearSource: () => void;
  setStatus: (status: StreamStatus) => void;
  setError: (error: string | null) => void;
  resendStill: () => void;
}

// Estado de geometria vacio. Se reusa en el valor inicial y en cada cambio de fuente
// para que las dos cosas no se puedan desincronizar.
const SIN_ZONAS = { zonas: [] as Zona[], zonasFrame: null, modoZona: 'off' as ModoZona };

// El volcado muere con la conexion, igual que las zonas: cambiar de fuente cierra el
// WebSocket y el backend cierra el archivo en su `finally`.
const SIN_EXPORT = {
  exportacion: { activa: false, archivo: null, filas: 0, error: null },
  exportNonce: { que: 'stop' as const, nonce: 0 },
};

export const useStreamStore = create<StreamState>((set) => ({
  source: { kind: 'none' },
  status: 'closed',
  lastError: null,
  stillNonce: 0,
  ...SIN_ZONAS,
  ...SIN_EXPORT,

  // Cambiar de fuente BORRA las zonas, y no es una cortesia: el poligono describe la
  // escena que se esta mirando, y la escena acaba de cambiar. Del otro lado pasa lo
  // mismo por construccion (el WS se cierra y la sesion muere con el).
  setCameraSource: (deviceId) =>
    set({ source: { kind: 'camera', deviceId }, lastError: null, ...SIN_ZONAS, ...SIN_EXPORT }),
  setFileVideo: (url) => set({ source: { kind: 'file-video', url }, lastError: null, ...SIN_ZONAS, ...SIN_EXPORT }),
  setFileImage: (url) => set({ source: { kind: 'file-image', url }, lastError: null, ...SIN_ZONAS, ...SIN_EXPORT }),
  clearSource: () => set({ source: { kind: 'none' }, ...SIN_ZONAS, ...SIN_EXPORT }),

  setZonas: (zonas, frame) =>
    set((s) => ({ zonas, zonasFrame: frame === undefined ? s.zonasFrame : frame })),
  setModoZona: (modoZona) => set({ modoZona }),
  zonaReset: { id: null, nonce: 0 },
  resetZona: (id = null) =>
    set((s) => ({ zonaReset: { id, nonce: s.zonaReset.nonce + 1 } })),

  pedirExport: (que) =>
    set((s) => ({ exportNonce: { que, nonce: s.exportNonce.nonce + 1 } })),
  setExportacion: (exportacion) => set({ exportacion }),

  setStatus: (status) => set({ status }),
  setError: (lastError) => set({ lastError }),

  resendStill: () => set((s) => ({ stillNonce: s.stillNonce + 1 })),
}));
