// diagnostics.ts — endpoints REST de confianza, metricas y logs.

import { api } from '@/shared/api/axios';

// POST /config/confidence { value: 0..1 } -> umbral en vivo.
export async function updateConfidence(value: number): Promise<number> {
  const { data } = await api.post<{ new_confidence?: number }>('/config/confidence', { value });
  return typeof data.new_confidence === 'number' ? data.new_confidence : value;
}

export interface Metrics {
  fps_avg: number;
  inf_avg_ms: number;
  avg_ms: number; // pipeline de inferencia (pre+inf+post), SIN el dibujo
  p95_ms: number;
  // Desde el 2026-08-26 el backend tambien DIBUJA (annotators + re-encode JPEG).
  // Va en su propio bucket para que ese costo no quede escondido dentro de post_ms:
  // es el numero que justifica —o no— haber mudado el render al backend.
  draw_avg_ms: number;
  avg_with_draw_ms: number; // lo que cuesta un frame de punta a punta en el backend
}

// GET /metrics -> { status, metrics }. Devuelve null si el backend no tiene datos.
export async function getMetrics(): Promise<Metrics | null> {
  const { data } = await api.get<{ status: string; metrics?: Metrics }>('/metrics');
  return data.status === 'ok' && data.metrics ? data.metrics : null;
}

export interface InferenceLog {
  timestamp: string;
  error: string;
}

// GET /logs/inference -> { logs: [{ timestamp, error }] } (ultimos 50).
export async function getInferenceLogs(): Promise<InferenceLog[]> {
  const { data } = await api.get<{ logs: InferenceLog[] }>('/logs/inference');
  return data.logs ?? [];
}
