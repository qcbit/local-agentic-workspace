import '@testing-library/jest-dom';

// Inject the VS Code API mock into the global Node/JSDOM window
(global as any).acquireVsCodeApi = jest.fn(() => ({
  postMessage: jest.fn(),
  getState: jest.fn(),
  setState: jest.fn()
}));
