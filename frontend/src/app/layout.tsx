import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "@/api-config";
import "./globals.css";
import { Providers } from "./providers";
import { ChatPanel } from "@/components/chat-panel";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    template: "%s · Rumil",
    default: "Rumil",
  },
  description: "Research workspace explorer",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <Providers>
          {children}
          <ChatPanel />
        </Providers>
      </body>
    </html>
  );
}
