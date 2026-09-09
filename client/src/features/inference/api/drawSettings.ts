// drawSettings.ts — push de los ajustes de dibujo al backend.
//
// Desde el 2026-08-26 el que dibuja es el BACKEND (paso 3 del plan del 2026-08-21),
// asi que necesita los colores. El cliente sigue siendo DUENO del estado y de su
// persistencia (localStorage, workspaceStore); esto solo empuja una copia.
//
// Se llama en dos momentos:
//   1. al cambiar un ajuste (DrawSettingsModal),
//   2. al cargar un modelo (ModelSelector), por si el backend se reinicio y volvio
//      a sus defaults mientras el cliente seguia vivo.
//
// Los cambios se aplican al frame SIGUIENTE: se perdio el cambio de color
// instantaneo que daba el dibujo client-side. Costo conocido y aceptado.
//
// OJO: el backend responde el estado EFECTIVO, que puede no ser el pedido — pedir
// suavizado o trazas prende el seguimiento, y apagar el seguimiento los apaga (la
// coherencia se fuerza en update_draw_config(), su unica puerta de escritura). El
// cliente aplica la misma regla ANTES de enviar (aplicarDependencias, en
// TrackingSettings.tsx) para que el panel no parpadee esperando la respuesta; el
// backend sigue siendo la autoridad y ambos convergen porque la regla es la misma.

import { api } from '@/shared/api/axios';
import type { DrawSettings } from '@/features/vision-workspace/services/types';

// El backend valida (#RRGGBB, rangos) y responde 422 si algo no cierra.
export interface DrawSettingsPayload {
  bboxColor?: string;
  labelColor?: string;
  maskAlpha?: number;
  boxStyle?: string;
  labelMode?: string;
  smartLabels?: boolean;
  shading?: boolean;
  shadingAlpha?: number;
  autoScale?: boolean;
  thickness?: number;
  textScale?: number;
  tracking?: boolean;
  smoothing?: boolean;
  smoothingLength?: number;
  traces?: boolean;
  tracesLength?: number;
  zoneColor?: string;
  zoneAnchor?: string;
  zoneTotal?: boolean;
  jpegQuality?: number;
}

export async function postDrawSettings(settings: DrawSettingsPayload): Promise<void> {
  await api.post('/config/draw', settings);
}

/**
 * Empuja los ajustes sin romper nada si falla: un backend caido o reiniciandose no
 * debe tumbar el flujo de "guardar colores" ni el de "cargar modelo". El unico
 * efecto de fallar es que el backend dibuja con los colores anteriores.
 */
export function pushDrawSettings(settings: DrawSettings): void {
  void postDrawSettings({
    bboxColor: settings.bboxColor,
    labelColor: settings.labelColor,
    maskAlpha: settings.maskAlpha,
    boxStyle: settings.boxStyle,
    labelMode: settings.labelMode,
    smartLabels: settings.smartLabels,
    shading: settings.shading,
    // shadingAlpha NO se manda: el cliente no expone la opacidad (no hay slider en
    // el panel) y omitirla deja el default del backend en pie. Ajustable por API.
    autoScale: settings.autoScale,
    tracking: settings.tracking,
    smoothing: settings.smoothing,
    smoothingLength: settings.smoothingLength,
    traces: settings.traces,
    tracesLength: settings.tracesLength,
    zoneColor: settings.zoneColor,
    zoneAnchor: settings.zoneAnchor,
    zoneTotal: settings.zoneTotal,
  }).catch((e) => console.warn('No se pudieron enviar los ajustes de dibujo:', e));
}
