import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import GlassBox from './GlassBox';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <GlassBox />
  </StrictMode>,
);
