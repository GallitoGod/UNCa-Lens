// ModelDropzone.tsx — importar modelos arrastrando archivos. Los File del browser se
// suben por multipart al backend (POST /models/upload); el frontend ya no toca disco
// ni resuelve paths (sin getPathForFile/IPC). El filtro de extension es solo UX: la
// garantia real (extension + nombre seguro) la aplica el backend antes de escribir.

import { useState } from 'react';
import { useImportModels } from '../hooks/useModelsList';
import { cn } from '@/shared/ui/cn';

// Espejo de MODEL_EXTENSIONS del backend (mainAPI.py). '.torchscript' es la extension
// con la que Ultralytics exporta TorchScript: el archivo es identico a un .pt exportado
// igual, pero sin tenerla en la lista el usuario tenia que renombrarlo a mano.
const SUPPORTED = new Set(['.onnx', '.tflite', '.h5', '.keras', '.torchscript', '.pt', '.pth']);

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i === -1 ? '' : name.slice(i).toLowerCase();
}

export function ModelDropzone() {
  const importModels = useImportModels();
  const [dragOver, setDragOver] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; ok: boolean } | null>(null);
  // Progreso del archivo en curso (nombre + % subido) mientras la mutacion corre.
  const [progress, setProgress] = useState<{ file: string; pct: number } | null>(null);

  async function onDrop(files: File[]) {
    const accepted = files.filter((f) => SUPPORTED.has(extOf(f.name)));
    const rejected = files.length - accepted.length;
    if (accepted.length === 0) {
      setFeedback({
        text: 'Formato no soportado (.onnx/.tflite/.h5/.keras/.torchscript/.pt/.pth)',
        ok: false,
      });
      return;
    }

    setFeedback(null);
    const res = await importModels.mutateAsync({
      files: accepted,
      onProgress: (p) => setProgress({ file: p.file, pct: Math.round(p.fraction * 100) }),
    });
    setProgress(null);

    // Resumen honesto: subidos, ignorados por extension, y fallidos en el backend.
    const okN = res.uploaded.length;
    const parts: string[] = [];
    if (okN) parts.push(okN === 1 ? '1 modelo agregado' : `${okN} modelos agregados`);
    if (rejected) parts.push(`${rejected} ignorado/s`);
    if (res.failed.length) parts.push(`${res.failed.length} con error`);
    setFeedback({
      text: parts.join(' · ') || 'Nada para importar',
      ok: okN > 0 && res.failed.length === 0,
    });
  }

  const busy = importModels.isPending;

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (!busy) void onDrop(Array.from(e.dataTransfer.files));
      }}
      className={cn(
        'flex h-full min-h-40 flex-col items-center justify-center gap-2 rounded-[var(--radius-lg)] border-2 border-dashed p-6 text-center transition-colors',
        dragOver ? 'border-accent bg-accent-soft' : 'border-border bg-control',
        busy && 'opacity-70',
      )}
    >
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-fg-subtle">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="17 8 12 3 7 8" />
        <line x1="12" y1="3" x2="12" y2="15" />
      </svg>
      <p className="text-sm text-fg">Arrastra un modelo aca</p>
      <p className="font-mono text-xs text-fg-subtle">.onnx · .tflite · .keras</p>

      {busy && progress && (
        <div className="w-full max-w-48">
          <p className="truncate font-mono text-xs text-fg-subtle">
            {progress.file} — {progress.pct}%
          </p>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-border">
            <div
              className="h-full rounded-full bg-accent transition-[width]"
              style={{ width: `${progress.pct}%` }}
            />
          </div>
        </div>
      )}

      {!busy && feedback && (
        <p className={cn('text-xs', feedback.ok ? 'text-success' : 'text-danger')}>{feedback.text}</p>
      )}
    </div>
  );
}
