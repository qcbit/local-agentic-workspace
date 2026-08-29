// 🎯 Define mocks at the very top level before any module imports occur
const mockPostMessage = jest.fn();
const mockApi = {
    postMessage: mockPostMessage,
    getState: jest.fn(),
    setState: jest.fn()
};

if (typeof window !== 'undefined') {
    (window as any).vscodeApi = mockApi;
    (window as any).acquireVsCodeApi = () => mockApi;
}

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { SettingsPanel } from './SettingsPanel';

describe('SettingsPanel Component', () => {
    beforeEach(() => {
        jest.clearAllMocks();
    });

    it('emits a ready command on mount', async () => {
        await act(async () => {
            render(<SettingsPanel />);
        });
        expect(mockPostMessage).toHaveBeenCalledWith({ command: 'ready' });
    });

    it('auto-populates max tokens when a known model is selected', async () => {
        await act(async () => {
            render(<SettingsPanel />);
        });

        act(() => {
            window.dispatchEvent(new MessageEvent('message', {
                data: {
                    command: 'loadModels',
                    models: ['qwen2.5-coder:14b', 'llama3:8b'],
                    specs: { 'qwen2.5-coder': { recommendedMax: 24000 } }
                }
            }));
            window.dispatchEvent(new MessageEvent('message', {
                data: {
                    command: 'loadConfig',
                    config: { active_profile: 'home' }
                }
            }));
        });

        const modelInput = document.querySelector('input[list="available-models-list"]') as HTMLInputElement;
        expect(modelInput).not.toBeNull();

        fireEvent.change(modelInput, { target: { value: 'qwen2.5-coder:14b' } });

        const tokenInput = screen.getByDisplayValue('24000') as HTMLInputElement;
        expect(tokenInput.value).toBe('24000');
    });

    it('sends the scanModelsFromPath command when directory button is clicked', async () => {
        await act(async () => {
            render(<SettingsPanel />);
        });
        
        act(() => {
            window.dispatchEvent(new MessageEvent('message', {
                data: { command: 'loadConfig', config: { active_profile: 'home' } }
            }));
        });

        const scanButton = screen.getByText('Scan Directory');
        fireEvent.click(scanButton);

        expect(mockPostMessage).toHaveBeenCalledWith({
            command: 'scanModelsFromPath',
            path: '~/.ollama/models/manifests/registry.ollama.ai/library'
        });
    });
});
