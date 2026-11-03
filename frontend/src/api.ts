import type {
  EngineConfig,
  SaveAlphaRequest,
  SavedAlphaInfo,
  SavedAlphaResults,
  SearchEvent,
  SearchRequest,
  SimulationRequest,
  SimulationResponse,
} from './types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export class AlphaExistsError extends Error {}

async function responseError(response: Response): Promise<Error> {
  let message = response.statusText || `HTTP ${response.status}`;
  try {
    const body: { error?: string } = await response.json();
    if (typeof body.error === 'string') message = body.error;
  } catch { /* Non-JSON transport errors use the HTTP status. */ }
  return new Error(message);
}

async function requestJson<T>(path: string, options: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

export function getConfig(signal?: AbortSignal): Promise<EngineConfig> {
  return requestJson('/api/config', { signal });
}

export function requestSimulation(request: SimulationRequest, signal?: AbortSignal): Promise<SimulationResponse> {
  return requestJson('/api/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal,
  });
}

export async function streamAlphaSearch(
  request: SearchRequest,
  onEvent: (event: SearchEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/ga/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error('Search response has no stream.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;
  const receive = (line: string) => {
    const event: SearchEvent = JSON.parse(line);
    if (event.type === 'search_complete' || event.type === 'error') completed = true;
    onEvent(event);
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline: number;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.trim()) receive(line);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) receive(buffer);
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (!completed) throw new Error('Search connection ended before completion.');
}

export async function saveAlpha(
  request: SaveAlphaRequest,
  signal?: AbortSignal,
): Promise<{ name: string; level: string; path?: string }> {
  const response = await fetch(`${API_BASE}/api/alphas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal,
  });
  if (!response.ok) {
    const error = await responseError(response);
    if (response.status === 409) throw new AlphaExistsError(error.message);
    throw error;
  }
  return response.json();
}

export function getAlphaList(signal?: AbortSignal): Promise<SavedAlphaInfo[]> {
  return requestJson('/api/alphas', { signal });
}

const alphaPath = (name: string) => `/api/alphas/${encodeURIComponent(name)}`;

export function getSavedResults(name: string, signal?: AbortSignal): Promise<SavedAlphaResults> {
  return requestJson(`${alphaPath(name)}/results`, { signal });
}

export function getSavedRow(name: string, id: number, signal?: AbortSignal): Promise<SimulationResponse> {
  return requestJson(`${alphaPath(name)}/results/rows/${id}`, { signal });
}

export function getSavedBatch(name: string, signal?: AbortSignal): Promise<SimulationResponse> {
  return requestJson(`${alphaPath(name)}/results/batch`, { signal });
}
