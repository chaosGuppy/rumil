import { NextRequest, NextResponse } from 'next/server';

const API_BASE = process.env.API_BASE_URL || 'http://localhost:8000';

export function middleware(request: NextRequest) {
  const password = process.env.AUTH_PASSWORD;

  if (password) {
    const auth = request.headers.get('Authorization') ?? '';
    let authenticated = false;
    if (auth.startsWith('Basic ')) {
      try {
        const decoded = atob(auth.slice(6));
        const [, pw] = decoded.split(':', 2);
        if (pw === password) authenticated = true;
      } catch {
        // fall through to 401
      }
    }
    if (!authenticated) {
      return new NextResponse('Unauthorized', {
        status: 401,
        headers: { 'WWW-Authenticate': 'Basic realm="rumil"' },
      });
    }
  }

  if (request.nextUrl.pathname.startsWith('/api/')) {
    const target = new URL(
      `${request.nextUrl.pathname}${request.nextUrl.search}`,
      API_BASE,
    );
    return NextResponse.rewrite(target);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|healthz).*)'],
};
