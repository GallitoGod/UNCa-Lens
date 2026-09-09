// ConfidenceSlider.tsx — umbral de confianza en vivo. Debounce para no inundar el
// backend (el viejo solo enviaba en 'change'; aca debounce en el arrastre).
//
// EL VALOR NO ES DE ESTE COMPONENTE, ES DEL BACKEND. Antes vivia como un
// `useState(50)` local: un 50% hardcodeado que no se enviaba al cargar ni se leia de
// ningun lado. El resultado era un control que MOSTRABA UN NUMERO QUE EL SISTEMA NO
// ESTABA USANDO — con `best` cargado el panel decia 50% mientras el backend filtraba a
// 0,15 (lo que declara su config), y mas de la mitad de las detecciones dibujadas y
// exportadas caian por debajo del numero que el panel afirmaba.
//
// Ahora el valor sale de workspaceStore, y ahi lo escribe quien carga el modelo con el
// umbral EFECTIVO que devuelve el backend. Cargar un modelo adopta el suyo en vez de
// imponerle el que hubiera en pantalla, y eso es deliberado: cada config esta calibrado
// aparte (best 0,15 por ser vista aerea con objetos chicos; efficientdet-lite0 0,50), y
// pisarlo con un valor generico dejaria a unos modelos ciegos y a otros llenos de ruido.

import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useUpdateConfidence } from '../hooks/useDiagnostics';
import { useStreamStore } from '../store/streamStore';
import { useWorkspaceStore } from '@/features/vision-workspace/store/workspaceStore';

export function ConfidenceSlider() {
  const confidence = useWorkspaceStore((s) => s.confidence); // [0,1] o null sin modelo
  const setConfidence = useWorkspaceStore((s) => s.setConfidence);
  const update = useUpdateConfidence();
  const timer = useRef<number | undefined>(undefined);
  const resendStill = useStreamStore((s) => s.resendStill);

  // Copia local SOLO para que el arrastre se vea fluido: el <input type=range> tiene
  // que responder a cada pixel, y el store se escribe con debounce junto con el envio.
  // Se re-sincroniza cuando el store cambia por afuera (al cargar un modelo).
  const [local, setLocal] = useState<number | null>(null);
  useEffect(() => {
    setLocal(confidence === null ? null : Math.round(confidence * 100));
  }, [confidence]);

  const sinModelo = confidence === null;
  const value = local ?? 0;

  function onChange(percent: number) {
    setLocal(percent);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      // El backend lee el umbral en CADA inferencia, asi que con camara/video el
      // cambio se ve en el frame siguiente. Con una imagen fija hay que pedir
      // explicitamente una inferencia nueva, si no la pantalla queda con el
      // resultado viejo y parece que el umbral no se respeta.
      update.mutate(percent / 100, {
        // Se adopta el EFECTIVO que responde el backend, no el pedido: es la misma
        // regla que ya siguen /config/draw y el ack de geometria, y evita que el
        // panel y el sistema puedan volver a decir cosas distintas.
        onSuccess: (efectivo) => {
          setConfidence(efectivo);
          resendStill();
        },
      });
    }, 200);
  }

  return (
    <div className="space-y-2.5">
      <div className="flex items-baseline justify-between">
        <span className="text-[12.5px] font-medium text-fg-muted">Confianza</span>
        <span className="font-mono text-xs font-semibold text-accent">
          {sinModelo ? '—' : `${value}%`}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        value={value}
        disabled={sinModelo}
        onChange={(e) => onChange(Number(e.target.value))}
        // --pct controla el fill cian del track (regla .range-cyan en index.css).
        style={{ '--pct': `${value}%` } as CSSProperties}
        className="range-cyan disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="Umbral de confianza"
      />
      {sinModelo && (
        // Sin modelo NO se muestra un numero cualquiera: no hay umbral vigente que
        // mostrar, y ese es justamente el bug que este archivo vino a arreglar.
        <p className="px-0.5 text-[11px] leading-snug text-label">
          Carga un modelo: el umbral lo trae su configuracion.
        </p>
      )}
    </div>
  );
}
