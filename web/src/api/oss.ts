import { request } from './client';

export interface ModelEndpointSettings {
  base_url: string;
  /** "********" means a key is stored; send it back unchanged to keep it. */
  api_key: string;
  model: string;
  /** Output budget for this deployment; null keeps the built-in ceiling. */
  max_output_tokens: number | null;
  /** "off" asks the provider to skip reasoning, which shares the same budget. */
  thinking: 'auto' | 'off';
}

export interface OssSettings {
  datasource_database_url: string;
  chat_model: ModelEndpointSettings;
  embedding_model: ModelEndpointSettings;
  configured: { datasource: boolean; chat_model: boolean; embedding_model: boolean };
}

export interface OssStatus {
  ready: boolean;
  error: string | null;
  login_required: boolean;
  project_id_prefix: string;
}

export const getOssStatus = () => request<OssStatus>('/api/oss/status');

export const getOssSettings = () => request<OssSettings>('/api/oss/settings');

export const saveOssSettings = (
  input: Pick<OssSettings, 'datasource_database_url' | 'chat_model' | 'embedding_model'>,
) =>
  request<OssSettings & { ready: boolean; error: string | null }>('/api/oss/settings', {
    method: 'PUT',
    body: input,
  });

export const testDatasource = (datasource_database_url: string) =>
  request<{ ok: true }>('/api/oss/settings/test-datasource', {
    method: 'POST',
    body: { datasource_database_url },
  });

export const testModel = (
  kind: 'chat_model' | 'embedding_model',
  endpoint: ModelEndpointSettings,
) =>
  request<{ ok: true; model?: string; dimension?: number }>('/api/oss/settings/test-model', {
    method: 'POST',
    body: { kind, endpoint },
  });
