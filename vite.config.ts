import { defineConfig } from 'vite';

// Served from https://hubertfong.github.io/bond-returns/
export default defineConfig({
  base: '/bond-returns/',
  build: { target: 'es2022' },
});
