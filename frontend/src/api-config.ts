import { client } from './api/client.gen';

const baseUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

client.setConfig({ baseUrl });
