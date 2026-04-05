import { apiJson } from './client'

export type ComponentStatus = 'healthy' | 'unhealthy' | 'degraded' | 'unknown'

export interface ComponentHealth {
  name: string
  status: ComponentStatus
  detail?: string | null
}

export interface HealthResponse {
  status: ComponentStatus
  model: string
  components: ComponentHealth[]
}

export async function getHealth(): Promise<HealthResponse> {
  return apiJson<HealthResponse>('/health')
}
