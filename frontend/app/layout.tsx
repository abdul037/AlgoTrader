import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AlgoTrader",
  description: "Trading support dashboard for stock signals, backtests, and guarded execution.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
