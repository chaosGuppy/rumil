import { NextRequest, NextResponse } from 'next/server';

export function middleware(request: NextRequest) {
  const password = process.env.AUTH_PASSWORD;
  if (!password) return NextResponse.next();

  const auth = request.headers.get('Authorization') ?? '';
  if (auth.startsWith('Basic ')) {
    try {
      const decoded = atob(auth.slice(6));
      const [, pw] = decoded.split(':', 2);
      if (pw === password) return NextResponse.next();
    } catch {
      // fall through to 401
    }
  }

  return new NextResponse('Unauthorized', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="rumil"' },
  });
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|healthz).*)'],
};
