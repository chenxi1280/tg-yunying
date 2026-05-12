export const BEIJING_TIME_ZONE = 'Asia/Shanghai';

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function hasExplicitTimezone(value: string): boolean {
  return /(?:Z|[+-]\d{2}:?\d{2})$/.test(value.trim());
}

export function parseBeijingDate(value?: string | null): Date | null {
  if (!value) return null;
  const normalized = value.trim().replace(' ', 'T');
  if (!normalized) return null;
  return new Date(hasExplicitTimezone(normalized) ? normalized : `${normalized}+08:00`);
}

export function formatBeijingDateTime(value?: string | Date | null): string {
  if (!value) return '-';
  const date = value instanceof Date ? value : parseBeijingDate(value);
  if (!date || Number.isNaN(date.getTime())) return '-';
  return dateTimeFormatter.format(date);
}

export function toBeijingDateTimeLocalValue(value?: string | null): string | undefined {
  if (!value) return undefined;
  const normalized = value.trim().replace(' ', 'T');
  if (!hasExplicitTimezone(normalized)) return normalized.slice(0, 16);
  const date = parseBeijingDate(normalized);
  if (!date) return undefined;
  const parts = Object.fromEntries(dateTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function fromBeijingDateTimeLocalValue(value?: string | null): string | null {
  if (!value) return null;
  const normalized = value.trim().replace(' ', 'T');
  return normalized.length === 16 ? `${normalized}:00` : normalized;
}
