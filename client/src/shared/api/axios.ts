// axios.ts — instancia HTTP unica del frontend.
// Reemplaza el constants.js con puerto hardcodeado y todos los fetch() sueltos.
// El interceptor de respuesta normaliza CUALQUIER error a ApiError (ver errors.ts),
// asi los hooks de TanStack Query reciben siempre el mismo tipo.

import axios from 'axios';
import { normalizeAxiosError } from './errors';

// Host/puerto configurables por entorno (Vite). Default = backend local en :8000.
const baseURL = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000';

/**
 * La misma base, para lo que NO pasa por axios: una descarga se dispara con un <a
 * download>, que necesita la URL completa y no una instancia con interceptores.
 */
export const API_BASE = baseURL;

export const api = axios.create({
  baseURL,
  timeout: 10_000,
  headers: { 'Content-Type': 'application/json' },
});

// Exito: pasa tal cual. Error: se rechaza con ApiError (nunca con el AxiosError crudo).
api.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(normalizeAxiosError(error)),
);
