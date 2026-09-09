// useVisionSession.ts — orquestador de la sesion de inferencia en vivo.
// Reacciona a la fuente activa (streamStore): maneja el media (getUserMedia / src),
// arranca el transporte WS y, por cada respuesta, repinta el frame y delega en la
// estrategia del modelo activo (presentFrame). Es el dueno del ciclo de vida.

import { useEffect, useRef, type RefObject } from 'react';
import { useStreamStore } from '../store/streamStore';
import { useUiStore } from '@/app/store/uiStore';
import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';
import { presentFrame, releaseOverlay } from '@/features/vision-workspace/services/present';
import {
  sendSingleFrame,
  startVideoStream,
  type StreamPayload,
  type VideoStreamHandle,
} from '../services/videoStream';
import { mensajeGeometria, type Ack, type MensajeGeometria } from '../services/geometry';

interface SessionRefs {
  videoRef: RefObject<HTMLVideoElement | null>;
  canvasRef: RefObject<HTMLCanvasElement | null>;
  overlayRef: RefObject<HTMLDivElement | null>;
}

export function useVisionSession({ videoRef, canvasRef, overlayRef }: SessionRefs) {
  const source = useStreamStore((s) => s.source);
  const activeView = useUiStore((s) => s.activeView);
  // Disparadores de re-inferencia para fuentes ESTATICAS (ver stillNonce en el store).
  const stillNonce = useStreamStore((s) => s.stillNonce);
  const activeType = useWorkspaceStore((s) => s.activeModel?.type ?? null);
  // Zonas de la escena. Cambian al dibujar o borrar una y hay que empujarlas por el
  // canal de control del WS (ver services/geometry.ts para por que no van por HTTP).
  const zonas = useStreamStore((s) => s.zonas);
  // Pedido de "pone la cuenta en cero" (ver zonaReset en streamStore).
  const zonaReset = useStreamStore((s) => s.zonaReset);
  // Pedido de empezar/terminar el volcado de detecciones (ver useDetectionExport).
  const exportNonce = useStreamStore((s) => s.exportNonce);

  // Refs vivos a la sesion en curso, para que el effect de navegacion pueda
  // pausar/reanudar SIN re-ejecutar el effect principal (que reconstruiria todo).
  const handleRef = useRef<VideoStreamHandle | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  // Frame fijo de la fuente 'file-image' y el render vigente, guardados para poder
  // volver a inferir sin re-ejecutar el effect principal (que revoca el objectURL).
  const stillRef = useRef<HTMLCanvasElement | null>(null);
  const renderRef = useRef<((payload: StreamPayload, src: HTMLCanvasElement) => void) | null>(null);
  // Ultima geometria armada, para que el camino one-shot de imagenes pueda re-mandarla
  // en cada envio (su WebSocket es efimero: nace sin zonas cada vez).
  const geometriaRef = useRef<MensajeGeometria | null>(null);

  useEffect(() => {
    if (!videoRef.current || !canvasRef.current || !overlayRef.current) return;
    if (source.kind === 'none') return;

    // Re-vinculo con tipo no-nulo explicito: asi los closures anidados (start /
    // cleanup / onload) ven elementos no nulos sin perder el narrowing.
    const video: HTMLVideoElement = videoRef.current;
    const canvas: HTMLCanvasElement = canvasRef.current;
    const overlay: HTMLDivElement = overlayRef.current;

    let handle: VideoStreamHandle | null = null;
    let mediaStream: MediaStream | null = null;
    let cancelled = false;

    // Render de un frame: lee modelo activo + colores en el momento (live).
    const render = (payload: StreamPayload, src: HTMLCanvasElement) => {
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const { activeModel, drawSettings } = useWorkspaceStore.getState();
      presentFrame({
        canvas,
        ctx,
        overlayRoot: overlay,
        source: src,
        payload,
        modelType: activeModel?.type ?? null,
        drawSettings,
      });
    };
    // Se expone para los re-envios de fuente estatica (effect de mas abajo).
    renderRef.current = render;

    const setStatus = useStreamStore.getState().setStatus;

    // El backend es la AUTORIDAD sobre la geometria: valida el poligono y responde el
    // estado efectivo. Si rechaza, el cliente se queda mostrando una zona que del otro
    // lado no existe, asi que como minimo hay que decirlo.
    const onAck = (ack: Ack) => {
      // El volcado: el backend es el unico que sabe si el archivo esta abierto de
      // verdad, asi que el estado del boton sale del ACK y no del click.
      if (ack.type === 'export_ack') {
        useStreamStore.getState().setExportacion({
          activa: ack.recording,
          archivo: ack.file ?? null,
          filas: ack.rows ?? 0,
          error: ack.error ?? null,
        });
        return;
      }
      // El backend es la AUTORIDAD sobre la geometria: si rechaza, el cliente se queda
      // mostrando una zona que del otro lado no existe, asi que como minimo hay que
      // decirlo.
      if (ack.error) console.warn('El backend rechazo la geometria:', ack.error);
    };

    // Alinea la pausa con la vista activa actual (leida fresh). Se llama al crear el
    // handle para cubrir la carrera de navegar antes de que getUserMedia resuelva.
    function syncToView() {
      const onInference = useUiStore.getState().activeView === 'inference';
      mediaStreamRef.current?.getVideoTracks().forEach((t) => (t.enabled = onInference));
      if (onInference) handleRef.current?.resume();
      else handleRef.current?.pause();
    }

    async function start() {
      try {
        if (source.kind === 'camera') {
          mediaStream = await navigator.mediaDevices.getUserMedia({
            video: {
              deviceId: { exact: source.deviceId },
              width: { ideal: 1280 },
              height: { ideal: 720 },
              frameRate: { ideal: 30 },
            },
          });
          if (cancelled) {
            mediaStream.getTracks().forEach((t) => t.stop());
            return;
          }
          mediaStreamRef.current = mediaStream;
          video.srcObject = mediaStream;
          video.muted = true;
          await video.play();
          // mirror:true -> espejo solo para camara.
          handle = startVideoStream({ videoElement: video, mirror: true, onMessage: render, onStatus: setStatus, onAck });
          handleRef.current = handle;
          syncToView(); // si arrancamos fuera de Inferencia, nacer en pausa
        } else if (source.kind === 'file-video') {
          video.srcObject = null;
          video.src = source.url;
          video.muted = true;
          video.loop = true;
          await video.play();
          handle = startVideoStream({ videoElement: video, mirror: false, onMessage: render, onStatus: setStatus, onAck });
          handleRef.current = handle;
          syncToView();
        } else if (source.kind === 'file-image') {
          // One-shot: cargar la imagen, dibujarla a un canvas temporal y mandar 1 frame.
          const img = new Image();
          img.onload = () => {
            if (cancelled) return;
            const tmp = document.createElement('canvas');
            tmp.width = img.naturalWidth;
            tmp.height = img.naturalHeight;
            tmp.getContext('2d')?.drawImage(img, 0, 0);
            // Se cachea para poder re-inferir el mismo frame al cambiar de modelo o
            // de umbral, sin volver a cargar la imagen ni tocar el objectURL.
            stillRef.current = tmp;
            sendSingleFrame(tmp, render, geometriaRef.current);
          };
          img.src = source.url;
        }
      } catch (err) {
        useStreamStore.getState().setError(err instanceof Error ? err.message : String(err));
      }
    }
    void start();

    // Cleanup: al cambiar de fuente o desmontar, cerrar todo y liberar recursos.
    // OJO: navegar a Modelos NO desmonta InferenceView (queda oculta), asi que este
    // cleanup NO corre al navegar — solo al cambiar de fuente o cerrar la app. Por eso
    // la pausa/reanudacion vive en el effect de abajo, no aca.
    return () => {
      cancelled = true;
      handle?.close();
      if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
      handleRef.current = null;
      mediaStreamRef.current = null;
      stillRef.current = null; // el frame fijo pertenece a ESTA fuente
      video.srcObject = null;
      video.removeAttribute('src');
      if (source.kind === 'file-video' || source.kind === 'file-image') {
        URL.revokeObjectURL(source.url);
      }
    };
  }, [source, videoRef, canvasRef, overlayRef]);

  // Navegacion (regla SDD 4.1.2): al salir de Inferencia se pausa la sesion (loop +
  // <video> + captura de camara) sin cerrar el WS ni soltar el permiso; al volver se
  // reanuda. Solo togglea la sesion existente; crearla/destruirla es del effect de arriba.
  useEffect(() => {
    const onInference = activeView === 'inference';
    mediaStreamRef.current?.getVideoTracks().forEach((t) => (t.enabled = onInference));
    if (onInference) handleRef.current?.resume();
    else handleRef.current?.pause();
  }, [activeView]);

  // Cambio de TIPO de modelo: soltar la capa de la estrategia anterior en el acto.
  // Con camara/video el proximo frame la limpiaria solo, pero con una imagen fija no
  // hay proximo frame y los labels quedaban pegados en pantalla.
  useEffect(() => {
    const canvas = canvasRef.current;
    const overlay = overlayRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !overlay || !ctx) return;
    releaseOverlay(overlay, {
      canvas,
      ctx,
      overlayRoot: overlay,
      frameWidth: canvas.width,
      frameHeight: canvas.height,
      settings: useWorkspaceStore.getState().drawSettings,
    });
  }, [activeType, canvasRef, overlayRef]);

  // Geometria de la escena: se empuja por el canal de control del WS cada vez que el
  // usuario dibuja o borra una zona.
  //
  // El tamano declarado sale del CANVAS, que es donde el usuario acaba de dibujar
  // (present.ts lo fija desde el bitmap que llega del backend). Es la mitad cliente de
  // la asercion de aspecto del backend: los dos miden lo mismo por su cuenta y tienen
  // que coincidir, porque el backend anota sobre el frame que le llego y no lo
  // redimensiona.
  //
  // El mensaje se manda SIEMPRE, incluso con la lista vacia: es declarativo y completo,
  // asi que la lista vacia es como se borra una zona del backend.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !canvas.width || !canvas.height) return;

    // COALESCIDO a proposito, y no es microoptimizacion: arrastrar un vertice escribe
    // en el store en CADA pointermove (~60/s), y del otro lado cada mensaje reconstruye
    // el sv.PolygonZone, que RASTERIZA una mascara del tamano del poligono. Sobre un
    // frame grande eso es del orden de megabytes por evento, sesenta veces por segundo,
    // compitiendo con los frames en el mismo canal. Ademas seria trabajo tirado: los
    // estados intermedios de un arrastre se pisan a los 16 ms.
    //
    // Los vertices igual se mueven en vivo (el editor los dibuja desde el store); lo
    // unico que espera es el redibujado del backend, y el ultimo estado SIEMPRE llega
    // porque el temporizador se reinicia con cada cambio y dispara al soltar.
    const t = setTimeout(() => {
      const msg = mensajeGeometria(canvas.width, canvas.height, zonas);
      geometriaRef.current = msg;
      handleRef.current?.sendGeometry(msg);
      // Con una imagen fija no hay frame siguiente donde se vea el cambio: hay que
      // volver a inferir. Camara y video refrescan solos.
      if (useStreamStore.getState().source.kind === 'file-image') {
        useStreamStore.getState().resendStill();
      }
    }, 60);
    return () => clearTimeout(t);
  }, [zonas, canvasRef]);

  // Reseteo del acumulado de una zona. Va SIN coalescer, al reves que la geometria:
  // es un pedido explicito y puntual del usuario, no el efecto colateral de un arrastre,
  // y esperar 60 ms a que "se calme" no tendria nada que esperar.
  useEffect(() => {
    if (zonaReset.nonce === 0) return;
    handleRef.current?.sendControl({ type: 'zone_reset', id: zonaReset.id });
  }, [zonaReset]);

  // Volcado de detecciones: empezar y terminar viajan por el canal de control, como
  // el reseteo de zona y por el mismo motivo (el archivo es de ESTA conexion). Bajarlo
  // no pasa por aca: es una descarga HTTP comun (ver useDetectionExport).
  useEffect(() => {
    if (exportNonce.nonce === 0) return;
    handleRef.current?.sendControl({
      type: exportNonce.que === 'start' ? 'export_start' : 'export_stop',
    });
  }, [exportNonce]);

  // Re-inferir el frame fijo cuando cambia un parametro (modelo, umbral). El envio
  // inicial lo hace el effect principal; aca solo se atienden los re-envios, por eso
  // se ignora el nonce 0.
  useEffect(() => {
    if (stillNonce === 0) return;
    if (source.kind !== 'file-image') return; // camara/video ya refrescan solos
    const still = stillRef.current;
    const render = renderRef.current;
    if (!still || !render) return;
    sendSingleFrame(still, render, geometriaRef.current);
  }, [stillNonce, source.kind]);
}
