/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: false,
  allowedDevOrigins: [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://0.0.0.0:3000",
  ],
  experimental: {
    optimizePackageImports: ['uplot'],
  },
};

// Keep live development assets separate from production builds and browser tests.
export default (phase) => ({
  ...nextConfig,
  distDir: process.env.NEXT_DIST_DIR || (phase === 'phase-development-server' ? '.next-dev' : '.next'),
});
