const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
export async function apiGet(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
export const search = (q) => apiGet(`/api/search?q=${encodeURIComponent(q)}`);
export const loadFullGraph = () => apiGet('/api/graph/full');
export const loadCrossCompany = () => apiGet('/api/graph/cross-company');
