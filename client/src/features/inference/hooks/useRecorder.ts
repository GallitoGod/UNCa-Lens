// useRecorder.ts — graba el canvas de salida a un archivo y lo descarga. La logica
// vive aca (hook) y Recorder.tsx queda como un boton fino.
//
// EL FORMATO SE ELIGE, NO SE ASUME (2026-09-09). Antes esto grababa siempre `.webm`,
// que es lo que MediaRecorder soportaba cuando se escribio, y arrastraba un parche
// —`fix-webm-duration`— porque el webm que produce MediaRecorder sale SIN duracion
// legible: no se puede hacer seek y los reproductores muestran duracion infinita.
//
// Medido en el runtime real (Electron 32 = Chromium 128, no en un navegador de
// escritorio, que puede diferir), grabando 2 s del mismo canvas:
//
//   video/mp4;codecs=avc1.42E01E   44.425 bytes   duracion leida: 1,976 s
//   video/webm;codecs=vp9          69.285 bytes   duracion leida: NULL
//
// O sea: con mp4 el problema de la duracion no se arregla, DEJA DE EXISTIR, el archivo
// pesa menos y ademas abre en cualquier reproductor y editor de Windows sin pedir VLC.
// El parche queda SOLO en el camino webm, que sigue existiendo como respaldo.
//
// La eleccion es por deteccion de capacidades y no por version: `isTypeSupported` es la
// unica fuente honesta —depende del build de Chromium y de los codecs del sistema— y
// asi un Electron mas viejo (o uno futuro que quite un codec) sigue grabando algo en
// vez de fallar.
//
// EL NOMBRE DEL ARCHIVO SE RECIBE (2026-09-10). Antes era la constante `grabacion.<ext>`,
// asi que la segunda grabacion caia en la carpeta de descargas como `grabacion (1).mp4` y
// no habia forma de saber de que modelo ni de que momento era. Ahora `start()` acepta el
// tronco, y quien lo elige es `useCaptura`: cuando en la misma captura se vuelcan las
// detecciones, le pasa EL MISMO tronco que el .json del backend, y el par queda unido por
// el nombre. Sin volcado cae a una marca de tiempo, que igual es mejor que una constante.

import { useCallback, useRef, useState, type RefObject } from 'react';
import fixWebmDuration from 'fix-webm-duration';

// captureStream existe en HTMLCanvasElement pero no siempre esta en los tipos DOM.
type CanvasWithCapture = HTMLCanvasElement & { captureStream(fps?: number): MediaStream };

interface Formato {
  mime: string;
  ext: string;
  /** Si hay que inyectarle la duracion a mano despues de grabar. */
  parcheDuracion: boolean;
}

// En orden de preferencia. El primero que el runtime soporte, gana.
//
// El avc1.42E01E explicito va PRIMERO y el "video/mp4" pelado despues: pedir el perfil
// (Baseline 3.0) es lo mas compatible que existe para reproducir en cualquier lado, y
// dejarlo librado al navegador puede darte un perfil que despues un editor no abre.
const FORMATOS: Formato[] = [
  { mime: 'video/mp4;codecs=avc1.42E01E', ext: 'mp4', parcheDuracion: false },
  { mime: 'video/mp4', ext: 'mp4', parcheDuracion: false },
  { mime: 'video/webm;codecs=vp9', ext: 'webm', parcheDuracion: true },
  { mime: 'video/webm', ext: 'webm', parcheDuracion: true },
];

/**
 * El mejor formato que este runtime puede grabar, o null si no puede ninguno.
 *
 * Exportada para poder verificarla desde afuera (y para que el panel pueda decir en
 * que formato va a grabar antes de apretar el boton).
 */
export function elegirFormato(): Formato | null {
  if (typeof MediaRecorder === 'undefined') return null;
  return FORMATOS.find((f) => MediaRecorder.isTypeSupported(f.mime)) ?? null;
}

export interface RecorderControls {
  recording: boolean;
  error: string | null;
  /** Extension del archivo que se va a producir ('mp4' | 'webm'), para mostrarla. */
  formato: string | null;
  /**
   * Arranca. `nombre` es el tronco del archivo SIN extension; omitirlo cae a una marca
   * de tiempo. Se usa para que el video y el volcado de detecciones de la misma captura
   * salgan con el mismo nombre (ver useCaptura).
   */
  start: (nombre?: string) => void;
  stop: () => void;
}

/** Tronco por defecto: marca de tiempo local, para que dos grabaciones no colisionen. */
function nombrePorDefecto(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `grabacion-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

export function useRecorder(canvasRef: RefObject<HTMLCanvasElement | null>): RecorderControls {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const startedAt = useRef(0); // performance.now() del inicio, para medir la duracion real
  // Formato con el que se esta grabando AHORA. Se fija al arrancar y no se relee: si
  // cambiara a mitad de una grabacion, los trozos no se podrian juntar en un blob.
  const formatoRef = useRef<Formato | null>(null);
  // Tronco del nombre de ESTA grabacion. Se fija al arrancar por el mismo motivo que el
  // formato: la descarga ocurre en `onstop`, y para entonces el argumento ya no esta.
  const nombreRef = useRef<string>('');

  const formato = elegirFormato();

  const download = useCallback((blob: Blob, ext: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${nombreRef.current || nombrePorDefecto()}.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
  }, []);

  const start = useCallback((nombre?: string) => {
    setError(null);
    const canvas = canvasRef.current as CanvasWithCapture | null;
    if (!canvas) {
      setError('No hay canvas de salida para grabar.');
      return;
    }
    const elegido = elegirFormato();
    if (!elegido) {
      setError('Este entorno no puede grabar video (MediaRecorder no disponible).');
      return;
    }

    let recorder: MediaRecorder;
    try {
      const stream = canvas.captureStream(30);
      recorder = new MediaRecorder(stream, { mimeType: elegido.mime });
    } catch (e) {
      setError(`No se pudo iniciar la grabacion: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }

    formatoRef.current = elegido;
    nombreRef.current = nombre || nombrePorDefecto();
    chunks.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.current.push(e.data);
    };
    recorder.onstop = () => {
      const usado = formatoRef.current ?? elegido;
      const raw = new Blob(chunks.current, { type: usado.mime });
      chunks.current = [];

      if (!usado.parcheDuracion) {
        // mp4: el moov sale completo, con duracion. Nada que parchear.
        download(raw, usado.ext);
        return;
      }
      const durationMs = performance.now() - startedAt.current;
      // Camino webm (respaldo): inyecta la duracion; si el parche falla, se descarga
      // el blob crudo igual — mejor un webm sin metadata que perder la grabacion.
      fixWebmDuration(raw, durationMs, { logger: false })
        .then((b) => download(b, usado.ext))
        .catch((e) => {
          console.warn('fix-webm-duration fallo, se descarga sin metadata:', e);
          download(raw, usado.ext);
        });
    };

    startedAt.current = performance.now();
    recorder.start();
    recorderRef.current = recorder;
    setRecording(true);
  }, [canvasRef, download]);

  const stop = useCallback(() => {
    recorderRef.current?.stop(); // dispara onstop -> (parche) + descarga
    recorderRef.current = null;
    setRecording(false);
  }, []);

  return { recording, error, formato: formato?.ext ?? null, start, stop };
}
