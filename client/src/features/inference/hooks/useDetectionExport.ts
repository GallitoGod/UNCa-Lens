// useDetectionExport.ts — volcado de las detecciones a un archivo, desde el cliente.
//
// POR QUE EXISTE: devuelve algo que el paso 3 se llevo. Hasta el 2026-08-26 el cliente
// recibia las filas [x1,y1,x2,y2,conf,cls] y podia hacer lo que quisiera con ellas;
// desde que el backend compone el frame, el cliente recibe PIXELES. Para alguien que use
// la app para trabajar, ese dato numerico ES el producto.
//
// COMO ESTA REPARTIDO, y por que asi:
//   - EMPEZAR y TERMINAR van por el WEBSOCKET, como la geometria de las zonas: lo que se
//     exporta son las detecciones de ESTA conexion, y un POST no sabria a cual le habla.
//   - BAJAR EL ARCHIVO va por HTTP. Un volcado puede tener cientos de miles de filas;
//     mandarlo por el canal de control seria empujar megabytes de JSON por donde viajan
//     los frames. El backend lo escribe a disco mientras graba y el cliente lo baja
//     despues, como cualquier descarga — igual que el video de useRecorder.
//
// La descarga se dispara sola al terminar, para que el gesto sea el mismo que el de
// grabar: apretas, pares, tenes el archivo.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useStreamStore } from '../store/streamStore';
import { API_BASE } from '@/shared/api/axios';

export interface ExportControls {
  /** Hay un volcado abierto en el backend. */
  exportando: boolean;
  error: string | null;
  /** Filas escritas en el ultimo volcado terminado (para poder decir cuantas fueron). */
  ultimasFilas: number | null;
  start: () => void;
  stop: () => void;
}

export function useDetectionExport(): ExportControls {
  const exportacion = useStreamStore((s) => s.exportacion);
  const pedirExport = useStreamStore((s) => s.pedirExport);
  // Para no volver a bajar el mismo archivo si el estado se re-emite.
  const bajado = useRef<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [ultimasFilas, setUltimasFilas] = useState<number | null>(null);

  // Al llegar el ack de cierre con un archivo, se dispara la descarga. Se hace aca y no
  // en el store porque tocar el DOM es cosa de la vista, no del estado.
  useEffect(() => {
    if (exportacion.error) {
      setError(exportacion.error);
      return;
    }
    setError(null);
    if (exportacion.activa || !exportacion.archivo) return;
    if (bajado.current === exportacion.archivo) return;
    bajado.current = exportacion.archivo;
    setUltimasFilas(exportacion.filas);

    // Sin filas no se baja nada: un archivo con `[]` no le sirve a nadie y abrir una
    // descarga vacia se lee como que algo fallo.
    if (!exportacion.filas) return;
    const a = document.createElement('a');
    a.href = `${API_BASE}/exports/${encodeURIComponent(exportacion.archivo)}`;
    a.download = exportacion.archivo;
    a.click();
  }, [exportacion]);

  const start = useCallback(() => {
    setError(null);
    setUltimasFilas(null);
    pedirExport('start');
  }, [pedirExport]);

  const stop = useCallback(() => pedirExport('stop'), [pedirExport]);

  return { exportando: exportacion.activa, error, ultimasFilas, start, stop };
}
