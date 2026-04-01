import { client } from './api/client.gen';

export const CLIENT_API_BASE =
  process.env.NEXT_PUBLIC_API_URL || '';

client.setConfig({ baseUrl: CLIENT_API_BASE });
