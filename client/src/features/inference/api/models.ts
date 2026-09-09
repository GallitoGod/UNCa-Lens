// models.ts — endpoints REST de modelos para la vista de inferencia.

import { api } from '@/shared/api/axios';
import type { ModelType } from '@/shared/api/types';

// GET /get_models -> { models: string[] } (configs con archivo de pesos).
export async function getModels(): Promise<string[]> {
  const { data } = await api.get<{ models: string[] }>('/get_models');
  return data.models;
}

// POST /select_model { model_name } -> carga + valida en el backend.
//
// Devuelve el umbral de confianza EFECTIVO con el que quedo el modelo. Es dato del
// backend a proposito: cada config declara el suyo (best 0.15 por ser vista aerea con
// objetos chicos, efficientdet-lite0 0.50) y el cliente no tiene por que adivinarlo ni
// imponerle uno. Sin esto el slider mostraba un 50% inventado mientras el sistema
// filtraba con otro numero.
export async function selectModel(modelName: string): Promise<number | null> {
  const { data } = await api.post<{ confidence?: number }>('/select_model', {
    model_name: modelName,
  });
  return typeof data.confidence === 'number' ? data.confidence : null;
}

// GET /configs/{name} -> model_type real del config (para enrutar la estrategia del
// workspace). null si el modelo no tiene config o no declara tipo.
export async function getModelType(name: string): Promise<ModelType | null> {
  const { data } = await api.get<{ config: { model_type?: ModelType } | null }>(`/configs/${name}`);
  return data.config?.model_type ?? null;
}
