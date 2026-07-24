import type { Metadata } from "next";
import { Geist, Geist_Mono, Noto_Serif_SC } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const notoSerifSc = Noto_Serif_SC({
  variable: "--font-serif-sc",
  weight: ["600", "700"],
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host") || "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") || (host.startsWith("localhost") ? "http" : "https");
  const image = `${protocol}://${host}/og-credit-score.png`;
  const title = "中建探员 · 信息中国信息采集";
  const description = "集中查看上海住建信用分、行政许可、行政处罚和官网证据的信息采集工作台。";
  return {
    title,
    description,
    openGraph: {
      title,
      description,
      type: "website",
      locale: "zh_CN",
      images: [{ url: image, width: 1732, height: 908, alt: "中建探员信息中国信息采集" }],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [image],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} ${notoSerifSc.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
