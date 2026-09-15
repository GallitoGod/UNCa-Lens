// useCaptura.ts — UN gesto, DOS registros: el video de salida y el volcado de
// detecciones. Orquesta useRecorder y useDetectionExport; ninguno de los dos se entera
// del otro (cada uno sigue siendo dueno de su mitad).
//
// POR QUE SE UNIFICO EL GESTO (2026-09-10, pedido del usuario). Eran dos botones
// hermanos e independientes, y el caso comun —querer las dos mitades del mismo tramo—
// costaba dos clicks. Pero el argumento fuerte NO es el click, es que ASI EL PAR NO SE
// PODIA ALINEAR: cada fila del volcado lleva `ms` medido desde el primer frame del
// export, y el video arranca su reloj en el `performance.now()` del suyo. Con dos
// clicks, entre el t=0 del video y el ms=0 del JSON hay un desfasaje igual a lo que el
// usuario tardo en apretar el segundo boton: segundos, desconocido, e IRRECUPERABLE
// despues. Con un gesto pasa a ser el ida y vuelta del ack (<100 ms). No es cero —el
// video lo captura el canvas del cliente y los `ms` los pone el backend— pero pasa de
// "no se" a "despreciable".
//
// POR QUE NO ES UN SOLO BOTON A SECAS, sino un boton y dos interruptores. Forzar las
// dos mitades tiene costo real en ambas direcciones: una sesion larga de solo datos
// arrastraria cientos de MB de video, y un video corto de demo arrastraria ~30 MB/min de
// JSON (con `best` sobre material aereo son ~70 detecciones por frame). Y sobre todo:
// LOS DOS FALLAN POR LADOS DISTINTOS. El video es local (MediaRecorder sobre el canvas);
// el volcado es del backend y su estado sale del ACK, no del click. Un indicador unico
// tendria que representar dos verdades, y si el archivo no se abriera diria "grabando"
// igual — el sintoma que este proyecto viene evitando en todas sus superficies. Por eso
// el gesto es uno y los BADGES siguen siendo dos.
//
// EL ORDEN IMPORTA: primero se pide el volcado y recien al llegar su ack arranca el
// video, porque el nombre del .json lo elige el BACKEND y el video se llama igual. Si el
// volcado falla, el video arranca lo mismo con una marca de tiempo: el usuario pidio
// video, y que le falte una mitad no es razon para quitarle la otra.

import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { useRecorder } from './useRecorder';
import { useDetectionExport } from './useDetectionExport';
import { useStreamStore } from '../store/streamStore';

/** Que mitades captura el boton. Es preferencia del usuario, asi que persiste. */
export interface QueCaptura {
  video: boolean;
  datos: boolean;
}

// Las dos prendidas: es el caso comun y el motivo de haber unificado el gesto.
const POR_DEFECTO: QueCaptura = { video: true, datos: true };

// Misma persistencia manual que uiStore/workspaceStore (sin middleware, por coherencia).
const CAPTURA_KEY = 'uncalens-captura';

function leerPreferencia(): QueCaptura {
  try {
    const raw = localStorage.getItem(CAPTURA_KEY);
    if (!raw) return POR_DEFECTO;
    return { ...POR_DEFECTO, ...(JSON.parse(raw) as Partial<QueCaptura>) };
  } catch {
    return POR_DEFECTO;
  }
}

/**
 * Cuanto se espera el ack del volcado antes de arrancar el video igual.
 *
 * No es paranoia: `useVisionSession` despacha el control con `handleRef.current?.send`,
 * o sea que si el WebSocket todavia no existe el mensaje se pierde EN SILENCIO y el ack
 * no llega nunca. Sin este limite, el video no arrancaria jamas y el boton se quedaria
 * en "arrancando" para siempre. Es la misma red de seguridad que el timeout de 3 s del
 * stream, con menos margen porque el ida y vuelta es local.
 */
const ESPERA_ACK_MS = 1500;

/** El tronco del .json (sin extension), que es como se va a llamar tambien el video. */
function troncoDe(archivo: string | null): string | undefined {
  if (!archivo) return undefined;
  return archivo.replace(/\.json$/i, '');
}

export interface CapturaControls {
  /** Hay una captura en curso o arrancando (lo que gobierna el boton). */
  activa: boolean;
  /** Se esta grabando video DE VERDAD (badge REC). */
  grabandoVideo: boolean;
  /** Hay un volcado abierto en el backend DE VERDAD (badge DATOS). */
  exportando: boolean;
  /** Que mitades pidio el usuario. */
  quiere: QueCaptura;
  setQuiere: (mitad: keyof QueCaptura, valor: boolean) => void;
  /** Extension del video que se va a producir ('mp4' | 'webm'). */
  formato: string | null;
  /** Filas del ultimo volcado terminado. */
  ultimasFilas: number | null;
  error: string | null;
  /** Si el boton puede arrancar algo (hay fuente y al menos una mitad prendida). */
  puedeArrancar: boolean;
  start: () => void;
  stop: () => void;
}

export function useCaptura(canvasRef: RefObject<HTMLCanvasElement | null>): CapturaControls {
  const {
    recording: grabandoVideo,
    error: errorVideo,
    formato,
    start: startVideo,
    stop: stopVideo,
  } = useRecorder(canvasRef);
  const {
    exportando,
    error: errorDatos,
    ultimasFilas,
    start: startDatos,
    stop: stopDatos,
  } = useDetectionExport();

  // El nombre que eligio el backend, para bautizar el video igual.
  const archivo = useStreamStore((s) => s.exportacion.archivo);
  const sourceKind = useStreamStore((s) => s.source.kind);
  // Igual que antes: sobre una foto suelta no hay nada que registrar en el tiempo, y la
  // conexion one-shot se cierra antes de que se pueda parar el volcado.
  const hayStream = sourceKind === 'camera' || sourceKind === 'file-video';

  const [quiere, setQuiereEstado] = useState<QueCaptura>(leerPreferencia);
  // Se pidio el volcado y se espera su ack para arrancar el video con su mismo nombre.
  const [arrancando, setArrancando] = useState(false);
  const timerRef = useRef<number | null>(null);
  // El estado deseado se lee dentro de callbacks diferidos (el timeout, el effect del
  // ack), donde el valor capturado en el closure podria estar viejo.
  const quiereRef = useRef(quiere);
  quiereRef.current = quiere;

  const limpiarTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const setQuiere = useCallback((mitad: keyof QueCaptura, valor: boolean) => {
    setQuiereEstado((previo) => {
      const next = { ...previo, [mitad]: valor };
      try {
        localStorage.setItem(CAPTURA_KEY, JSON.stringify(next));
      } catch {
        // localStorage lleno/deshabilitado: el cambio sigue valiendo en memoria.
      }
      return next;
    });
  }, []);

  const start = useCallback(() => {
    if (quiere.datos) {
      // Primero el volcado: su ack trae el nombre del que despues cuelga el del video.
      setArrancando(true);
      startDatos();
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        setArrancando(false);
        if (quiereRef.current.video) startVideo(); // sin ack no hay tronco compartido
      }, ESPERA_ACK_MS);
      return;
    }
    if (quiere.video) startVideo();
  }, [quiere.datos, quiere.video, startDatos, startVideo]);

  const stop = useCallback(() => {
    limpiarTimer();
    setArrancando(false);
    if (grabandoVideo) stopVideo();
    if (exportando) stopDatos();
  }, [limpiarTimer, grabandoVideo, exportando, stopVideo, stopDatos]);

  // Llego la respuesta del backend (abrio el archivo, o fallo): arranca el video. Se
  // hace en un effect y no en el `start` porque el ack es asincrono, y se mira tanto el
  // exito como el error: un volcado que no abrio no tiene que dejar al video sin grabar.
  useEffect(() => {
    if (!arrancando) return;
    if (!exportando && !errorDatos) return;
    limpiarTimer();
    setArrancando(false);
    if (quiereRef.current.video) startVideo(troncoDe(archivo));
  }, [arrancando, exportando, errorDatos, archivo, startVideo, limpiarTimer]);

  // Si la fuente se va a mitad de una grabacion, se corta y se descarga lo que haya. El
  // volcado se cierra solo (cambiar de fuente cierra el WebSocket y el backend cierra el
  // archivo en su `finally`), pero el video es local y seguiria grabando un canvas
  // muerto — con el boton unificado eso ademas lo dejaria diciendo "grabando" cuando ya
  // no queda nada que grabar.
  useEffect(() => {
    if (hayStream || !grabandoVideo) return;
    stopVideo();
  }, [hayStream, grabandoVideo, stopVideo]);

  // Al desmontar, que no quede un timeout apuntando a un componente que ya no esta.
  useEffect(() => limpiarTimer, [limpiarTimer]);

  return {
    // `arrancando` cuenta como activa a proposito: el boton refleja la INTENCION en
    // vuelo (y asi un segundo click cancela en vez de arrancar otra captura), mientras
    // que los badges de abajo siguen reflejando lo que esta pasando DE VERDAD.
    activa: grabandoVideo || exportando || arrancando,
    grabandoVideo,
    exportando,
    quiere,
    setQuiere,
    formato,
    ultimasFilas,
    error: errorVideo ?? errorDatos,
    puedeArrancar: hayStream && (quiere.video || quiere.datos),
    start,
    stop,
  };
}
