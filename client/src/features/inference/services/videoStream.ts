// videoStream.ts — transporte WS de inferencia (framework-agnostico, sin React).
// Porte fiel del streamHandler.js viejo. Conserva sus invariantes criticas:
//   - 1 frame en vuelo (no se envia el proximo hasta recibir respuesta)
//   - anti-deadlock (timeout de 3s libera la espera)
//   - el frame enviado queda intacto en captureCanvas para repintarlo
//   - reconexion con backoff exponencial (salvo cierre intencional)
//   - mirror aplicado en la captura, solo cuando lo pide la camara

import { STREAM_URL } from '@/shared/api/ws';
import {
  esAck,
  type Ack,
  type MensajeControl,
  type MensajeGeometria,
} from './geometry';

export type StreamStatus = 'connecting' | 'open' | 'closed' | 'waiting';

/**
 * Lo que devuelve el backend por cada frame, en UNA de dos formas (paso 3 del plan
 * del 2026-08-21, implementado el 2026-08-26):
 *
 *   'frame' -> BINARIO: el JPEG ya compuesto por el backend (deteccion/segmentacion).
 *              El cliente solo lo pinta: no parsea, no dibuja, no sabe de cajas.
 *   'json'  -> TEXTO: el envelope {task, result, error} de siempre. Lo usan
 *              clasificacion (su resultado es texto, no geometria) y TODOS los
 *              errores, de cualquier tarea.
 *
 * Se discrimina por el TIPO del dato recibido, no por un campo del mensaje.
 */
export type StreamPayload =
  | { kind: 'json'; envelope: unknown }
  | { kind: 'frame'; bitmap: ImageBitmap };

// Decodifica el frame compuesto. Devuelve null si el blob no es una imagen valida:
// un frame roto no debe matar el loop.
async function decodeFrame(data: Blob | ArrayBuffer): Promise<ImageBitmap | null> {
  const blob = data instanceof Blob ? data : new Blob([data], { type: 'image/jpeg' });
  try {
    return await createImageBitmap(blob);
  } catch (e) {
    console.warn('No se pudo decodificar el frame compuesto del backend:', e);
    return null;
  }
}

export interface VideoStreamHandle {
  /**
   * Manda la geometria de la escena (zonas) por el canal de control del WS.
   *
   * Va por el WEBSOCKET y no por HTTP porque la zona pertenece a ESTA conexion: un
   * POST no sabria a que WebSocket le habla, y ademas competiria con los frames en
   * vuelo (no estaria definido si el frame N se compone con la zona vieja o la
   * nueva). Por el mismo canal, el orden ES el orden.
   *
   * El ultimo mensaje se RECUERDA y se re-emite en cada reconexion. El spec aceptaba
   * como agujero conocido que una reconexion por backoff perdiera la zona sin que el
   * usuario hubiera cambiado de fuente; como el cliente tiene que guardar el poligono
   * igual (lo necesita para dibujar el editor), cerrar ese agujero sale gratis. NO es
   * persistencia: sigue muriendo con la fuente.
   */
  sendGeometry(mensaje: MensajeGeometria): void;
  /**
   * Manda un control que NO define estado re-emitible (hoy: poner en cero el acumulado
   * de una zona). No se recuerda: una conexion nueva ya nace con la cuenta en cero, asi
   * que re-emitirlo al reconectar borraria una cuenta que recién empieza.
   */
  sendControl(mensaje: MensajeControl): void;
  // Pausa el envio de frames y el <video> SIN cerrar el WS ni soltar la camara
  // (para navegar a otra vista y volver sin reconectar ni repedir permisos).
  pause(): void;
  // Reanuda tras pause(): vuelve a reproducir y reinicia el loop si el WS sigue abierto.
  resume(): void;
  close(): void;
}

export interface VideoStreamOptions {
  videoElement: HTMLVideoElement;
  mirror?: boolean;
  onMessage: (payload: StreamPayload, captureCanvas: HTMLCanvasElement) => void;
  onStatus?: (status: StreamStatus) => void;
  /** Respuesta a un mensaje de control (geometria, reseteo, volcado). No es un frame. */
  onAck?: (ack: Ack) => void;
}

const RESPONSE_TIMEOUT_MS = 3000; // red de seguridad: nunca esperar para siempre

export function startVideoStream(opts: VideoStreamOptions): VideoStreamHandle {
  const { videoElement, mirror = false, onMessage, onStatus, onAck } = opts;

  const captureCanvas = document.createElement('canvas');
  const captureCtx = captureCanvas.getContext('2d');

  let ws: WebSocket | null = null;
  let animationFrameId: number | null = null;
  let waitingForResponse = false;
  let waitingSince = 0;
  let intentionallyClosed = false;
  let paused = false; // navegacion fuera de Inferencia: loop detenido, WS vivo
  let retryDelay = 1000;
  let lastResponseSeq = 0; // ordena los decodes asincronos de frames compuestos
  // Ultima geometria enviada. Se re-emite al (re)conectar: la sesion del backend vive
  // en la conexion, asi que un WS nuevo nace sin zonas.
  let geometria: MensajeGeometria | null = null;

  function enviarGeometria() {
    if (geometria && ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(geometria));
  }

  function connect() {
    onStatus?.('connecting');
    ws = new WebSocket(STREAM_URL);

    ws.onopen = () => {
      retryDelay = 1000;
      waitingForResponse = false;
      onStatus?.('open');
      // ANTES del primer frame: si la zona llegara despues, el frame de apertura se
      // compondria sin ella y el usuario veria parpadear el poligono al reconectar.
      enviarGeometria();
      // Si reconectamos estando en pausa (navegacion fuera de Inferencia), no
      // arrancamos el loop: lo hara resume() al volver.
      if (!paused) startFrameLoop();
    };

    ws.onclose = () => {
      if (animationFrameId !== null) cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
      if (!intentionallyClosed) {
        onStatus?.('connecting');
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 10000);
      } else {
        onStatus?.('closed');
      }
    };

    ws.onerror = (err) => console.error('WebSocket error:', err);

    ws.onmessage = (event) => {
      // TEXTO: envelope JSON (clasificacion o error) o ACK de un control.
      if (typeof event.data === 'string') {
        let envelope: unknown;
        try {
          envelope = JSON.parse(event.data);
        } catch {
          console.warn('Respuesta de texto del stream no es JSON valido');
          waitingForResponse = false;
          return;
        }
        // OJO: el ack NO libera waitingForResponse. Es la respuesta al mensaje de
        // control, no al frame en vuelo; soltar la espera aca pondria dos frames en
        // vuelo y romperia la invariante de uno por vez.
        if (esAck(envelope)) {
          onAck?.(envelope);
          return;
        }
        waitingForResponse = false;
        // captureCanvas sigue con el frame que se envio (1 en vuelo): el consumidor
        // lo repinta y superpone su capa.
        onMessage({ kind: 'json', envelope }, captureCanvas);
        return;
      }

      waitingForResponse = false;

      // BINARIO: frame ya compuesto por el backend. El decode es ASINCRONO, asi que
      // se numera la respuesta: si mientras decodificabamos llego una mas nueva,
      // este bitmap se descarta en vez de pintar un frame viejo encima del actual.
      const seq = ++lastResponseSeq;
      void decodeFrame(event.data as Blob).then((bitmap) => {
        if (!bitmap) return;
        if (seq !== lastResponseSeq) {
          bitmap.close(); // llego uno mas nuevo: soltar la memoria del viejo
          return;
        }
        onMessage({ kind: 'frame', bitmap }, captureCanvas);
      });
    };
  }

  function startFrameLoop() {
    function tick() {
      // Anti-deadlock: si el backend no respondio a tiempo, soltar la espera.
      if (waitingForResponse && performance.now() - waitingSince > RESPONSE_TIMEOUT_MS) {
        console.warn('Stream: respuesta demorada, se reanuda el envio');
        waitingForResponse = false;
      }

      if (
        !waitingForResponse &&
        ws?.readyState === WebSocket.OPEN &&
        videoElement.readyState >= HTMLMediaElement.HAVE_ENOUGH_DATA
      ) {
        const vw = videoElement.videoWidth;
        const vh = videoElement.videoHeight;
        if (vw > 0 && vh > 0 && captureCtx) {
          captureCanvas.width = vw;
          captureCanvas.height = vh;
          if (mirror) {
            // Espejo SOLO camara: aca, en el cliente, asi los archivos no se espejan.
            captureCtx.save();
            captureCtx.scale(-1, 1);
            captureCtx.drawImage(videoElement, -vw, 0);
            captureCtx.restore();
          } else {
            captureCtx.drawImage(videoElement, 0, 0);
          }

          waitingForResponse = true;
          waitingSince = performance.now();
          onStatus?.('waiting');
          captureCanvas.toBlob(
            (blob) => {
              if (blob && ws?.readyState === WebSocket.OPEN) {
                ws.send(blob); // binario: sin overhead de base64
              } else {
                waitingForResponse = false;
              }
            },
            'image/jpeg',
            0.8,
          );
        }
      }
      animationFrameId = requestAnimationFrame(tick);
    }
    tick();
  }

  connect();

  return {
    sendGeometry(mensaje: MensajeGeometria) {
      geometria = mensaje;
      enviarGeometria();
    },
    sendControl(mensaje: MensajeControl) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(mensaje));
    },
    pause() {
      if (paused) return;
      paused = true;
      if (animationFrameId !== null) cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
      waitingForResponse = false; // soltar cualquier frame en vuelo
      videoElement.pause();
    },
    resume() {
      if (!paused) return;
      paused = false;
      // play() puede rechazar (autoplay); no es fatal para reanudar el loop.
      void videoElement.play().catch(() => {});
      // Si el WS sigue abierto reiniciamos el loop; si se cayo, el backoff lo
      // reconectara y onopen lo arrancara (paused ya es false).
      if (ws?.readyState === WebSocket.OPEN && animationFrameId === null) {
        startFrameLoop();
      }
    },
    close() {
      intentionallyClosed = true;
      if (animationFrameId !== null) cancelAnimationFrame(animationFrameId);
      ws?.close();
    },
  };
}

// Envio one-shot para imagenes: abre un WS efimero, manda un frame y cierra.
export function sendSingleFrame(
  sourceCanvas: HTMLCanvasElement,
  onResult: (payload: StreamPayload, sourceCanvas: HTMLCanvasElement) => void,
  geometria?: MensajeGeometria | null,
): void {
  // stateful=false: una foto suelta NO es una secuencia. Sin esto el backend armaria
  // la memoria de sesion (tracker, suavizado) para un unico frame que no tiene con
  // que compararse. Se declara en vez de deducirse: desde el backend, una conexion
  // que todavia no recibio su segundo frame es identica a una que nunca lo va a recibir.
  const ws = new WebSocket(`${STREAM_URL}?stateful=false`);

  ws.onopen = () => {
    // La geometria va PRIMERO y por conexion: este WS es efimero, asi que la zona
    // hay que re-declararla en cada envio. Que las zonas sigan funcionando sobre una
    // foto suelta es a proposito — `stateful=false` declara que no hay memoria
    // TEMPORAL que construir (tracking), no que no haya escena: contar vehiculos en
    // una region de una imagen es un uso legitimo.
    if (geometria) ws.send(JSON.stringify(geometria));
    sourceCanvas.toBlob(
      (blob) => {
        if (blob) ws.send(blob);
        else ws.close();
      },
      'image/jpeg',
      0.9,
    );
  };

  // Mismas dos formas que el stream continuo: texto (envelope) o binario (frame ya
  // compuesto). Aca hay un solo frame en juego, asi que no hace falta ordenar nada.
  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      let envelope: unknown;
      try {
        envelope = JSON.parse(event.data);
      } catch {
        ws.close();
        return;
      }
      // El ack del control llega ANTES que la respuesta al frame (un mensaje por
      // mensaje). Cerrar aca dejaria la foto sin inferir.
      if (esAck(envelope)) {
        if (envelope.error) console.warn('Zona rechazada por el backend:', envelope.error);
        return;
      }
      onResult({ kind: 'json', envelope }, sourceCanvas);
      ws.close();
      return;
    }

    void decodeFrame(event.data as Blob).then((bitmap) => {
      if (bitmap) onResult({ kind: 'frame', bitmap }, sourceCanvas);
      ws.close();
    });
  };

  ws.onerror = (err) => console.error('WS error al procesar imagen:', err);
}
