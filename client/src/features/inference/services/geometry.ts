// geometry.ts — la geometria de la escena del lado del cliente (zonas poligonales).
//
// DOS COSAS QUE PARECEN UNA Y NO LO SON, y separarlas es la decision de fondo:
//
//   - El COLOR de la zona y el ANCLAJE son ajustes del usuario: viven en
//     drawSettings, persisten en localStorage y viajan por POST /config/draw.
//   - El POLIGONO describe LA ESCENA: vive en streamStore, muere cuando cambia la
//     fuente y NO se persiste. Persistirlo obligaria a identificar la fuente con una
//     clave, y el nombre de archivo es fragil ("Prueba.mp4" y "Prueba_4x3.mp4" son
//     escenas distintas, y renombrar cualquiera rompe la asociacion).
//
// COORDENADAS NORMALIZADAS [0,1], fracciones del ancho y del alto. El motivo NO es
// portabilidad entre resoluciones (sin persistencia no hay nada que portar): es que
// asi ESTE lado no necesita saber la resolucion del frame. La conversion de un click
// colapsa a (clientX - rect.left) / rect.width, sin canvas.width en ninguna parte.
// (El editor SI multiplica al DIBUJAR, por px de la caja: ver el encabezado de
// ZoneEditor.tsx para por que el viewBox de 1x1 que proponia el spec no servia.)
//
// EL CANAL: el poligono viaja por el propio WebSocket, no por HTTP. La zona pertenece
// a UNA conexion (muere con ella) y un POST no sabe a que WebSocket le esta hablando;
// ademas, por el mismo canal no hay carrera con los frames en vuelo — el orden ES el
// orden. Ver render/geometry.py y el spec 2026-08-28 §5.

/** Un punto en coordenadas normalizadas: [x, y], ambos en [0,1]. */
export type Punto = [number, number];

/** Un poligono de la escena. El id lo genera el cliente y el backend lo devuelve tal cual. */
export interface Zona {
  id: string;
  puntos: Punto[];
}

/** Mensaje de control que el cliente manda por el WS. */
export interface MensajeGeometria {
  type: 'geometry';
  /** Sobre que tamano de frame se dibujo (§4.1 del spec): dos enteros, no el cociente. */
  frame: { w: number; h: number };
  zones: { id: string; points: Punto[] }[];
}

/**
 * Mensaje de control que pone en cero el acumulado de una zona (o de todas, sin `id`).
 *
 * Va por el mismo canal que la geometria y por la misma razon: el contador vive en ESTA
 * conexion. A diferencia del mensaje de geometria, este NO se recuerda para re-emitirlo
 * al reconectar — una conexion nueva ya nace con la cuenta en cero.
 */
export interface MensajeResetZona {
  type: 'zone_reset';
  id?: string | null;
}

/**
 * Empezar o terminar el volcado de detecciones a disco.
 *
 * Va por el WS y no por HTTP por lo mismo que la geometria: lo que se exporta son las
 * detecciones de ESTA conexion. Bajar el archivo, en cambio, si va por HTTP — puede
 * pesar megabytes y no tiene por que competir con los frames.
 */
export interface MensajeExport {
  type: 'export_start' | 'export_stop';
}

/** Cualquier mensaje de control que el cliente manda por el WS. */
export type MensajeControl = MensajeGeometria | MensajeResetZona | MensajeExport;


// ── Los acks ──────────────────────────────────────────────────────────────────
// Union DISCRIMINADA por `type` y no un objeto laxo: por el canal de control ya viajan
// tres respuestas distintas, y el dia que se agregue una cuarta el compilador tiene que
// obligar a atenderla en vez de dejarla pasar como `unknown`.

/** `error` != null significa que se conservo la geometria anterior. */
export interface AckGeometria {
  type: 'geometry_ack';
  zones: number;
  lines: number;
  error: string | null;
}

export interface AckResetZona {
  type: 'zone_reset_ack';
  zone: string | null;
  error: string | null;
}

/** Estado efectivo del volcado de detecciones de esta conexion. */
export interface AckExport {
  type: 'export_ack';
  recording: boolean;
  file?: string | null;
  rows?: number;
  frames?: number;
  error?: string | null;
}

/** Un control que el backend no supo interpretar. */
export interface AckControlDesconocido {
  type: 'control_error';
  error: string;
}

export type Ack = AckGeometria | AckResetZona | AckExport | AckControlDesconocido;

/** Minimo de vertices para que un poligono encierre algo (coincide con el backend). */
export const MIN_VERTICES = 3;
/** Tope de zonas simultaneas (coincide con MAX_ZONAS de render/geometry.py). */
export const MAX_ZONAS = 8;

/**
 * El mensaje declarativo COMPLETO: siempre va toda la geometria vigente, nunca un
 * delta. Un delta obligaria a las dos puntas a coincidir sobre un historial, que es
 * justo el acoplamiento que este proyecto viene evitando en el resto de la superficie.
 */
export function mensajeGeometria(frameW: number, frameH: number, zonas: Zona[]): MensajeGeometria {
  return {
    type: 'geometry',
    frame: { w: Math.round(frameW), h: Math.round(frameH) },
    zones: zonas.map((z) => ({ id: z.id, points: z.puntos })),
  };
}

/** Si el mensaje que llego por el WS es la respuesta a un control y no a un frame. */
export function esAck(mensaje: unknown): mensaje is Ack {
  // El envelope de un frame es {task, result, error}: no tiene 'type'. El discriminador
  // es el simetrico del que usa el backend (_decode_control en mainAPI.py).
  return (
    typeof mensaje === 'object' &&
    mensaje !== null &&
    typeof (mensaje as { type?: unknown }).type === 'string'
  );
}

/** Id corto y unico dentro de la escena. No sale del cliente mas que en el mensaje. */
export function nuevoIdDeZona(existentes: Zona[]): string {
  let n = existentes.length + 1;
  const usados = new Set(existentes.map((z) => z.id));
  while (usados.has(`z${n}`)) n += 1;
  return `z${n}`;
}

/** Area del poligono (formula del cordon de zapato), para descartar los degenerados. */
export function area(puntos: Punto[]): number {
  let acc = 0;
  for (let i = 0; i < puntos.length; i += 1) {
    const [x1, y1] = puntos[i];
    const [x2, y2] = puntos[(i + 1) % puntos.length];
    acc += x1 * y2 - x2 * y1;
  }
  return Math.abs(acc) / 2;
}

/**
 * Si el poligono es aceptable. El backend valida igual y es la autoridad; esto existe
 * para no ofrecerle al usuario un boton "Confirmar" que va a fallar del otro lado.
 */
export function zonaValida(puntos: Punto[]): boolean {
  return puntos.length >= MIN_VERTICES && area(puntos) > 1e-6;
}

/**
 * La caja de contenido del canvas RELATIVA a su contenedor posicionado.
 *
 * El canvas se centra con `max-h-full max-w-full` y sin width/height en CSS, asi que
 * el navegador le conserva el aspecto del bitmap y su rect ES la imagen. Pero el
 * overlayRoot es `absolute inset-0` sobre TODO el workspace, que es mas grande. Sin
 * esto el SVG del editor quedaria estirado sobre las bandas vacias y cada click
 * caeria unos pixeles corrido.
 *
 * Se deriva midiendo, no asumiendo: si algun dia el CSS fuerza width y height a la
 * vez, object-contain empezaria a poner bandas ADENTRO del elemento y la formula
 * ingenua se romperia en silencio.
 */
export function cajaDelCanvas(canvas: HTMLCanvasElement, contenedor: HTMLElement) {
  const c = canvas.getBoundingClientRect();
  const p = contenedor.getBoundingClientRect();
  const aspectoBitmap = canvas.width / canvas.height;
  const aspectoCaja = c.width / c.height;

  // Con object-contain, si los aspectos no coinciden hay bandas adentro del elemento.
  let w = c.width;
  let h = c.height;
  if (Number.isFinite(aspectoBitmap) && aspectoBitmap > 0 && Number.isFinite(aspectoCaja)) {
    if (aspectoCaja > aspectoBitmap) w = c.height * aspectoBitmap;
    else h = c.width / aspectoBitmap;
  }
  return {
    left: c.left - p.left + (c.width - w) / 2,
    top: c.top - p.top + (c.height - h) / 2,
    width: w,
    height: h,
  };
}
