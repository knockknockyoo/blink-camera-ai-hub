import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Blink Camera AI Hub · 로컬 AI 카메라 모니터",
  description: "Blink 영상을 5분마다 확인하고 사람, 동물, 이상징후를 정리하는 로컬 대시보드",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
