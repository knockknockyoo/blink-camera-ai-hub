import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Blink Camera AI Hub · Local AI Camera Monitor",
  description: "A local dashboard that checks Blink videos every five minutes and organizes important events.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
