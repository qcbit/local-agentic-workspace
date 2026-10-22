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

    // Regression test (Part A): a hand-edited `compaction` block in
    // .agentic_config.json must survive a save that only edits the
    // primary LLM fields, and any other undocumented key must round-trip.
    it('preserves hand-edited compaction block and unknown keys on save (lossless round-trip)', async () => {
        const seedProfile = {
            llm: {
                endpoint_url: 'http://127.0.0.1:11434/v1/chat/completions',
                model_name: 'qwen3-coder:30b',
                api_key: '',
                models_path: '~/.ollama/models/manifests/registry.ollama.ai/library',
                chars_per_token: 4
            },
            memory: { max_tokens: 240000 },
            // Hand-edited block that the old code would wipe on save:
            compaction: {
                model_name: 'qwen2.5:1.5b',
                endpoint_url: 'http://127.0.0.1:11434/v1/chat/completions'
            },
            // Undocumented forward-compat key (any of these should survive):
            experimental: {
                speculative_decoding: true
            }
        };

        await act(async () => {
            render(<SettingsPanel />);
        });

        act(() => {
            window.dispatchEvent(new MessageEvent('message', {
                data: {
                    command: 'loadConfig',
                    config: {
                        active_profile: 'qwen3-coder',
                        profiles: { 'qwen3-coder': seedProfile }
                    }
                }
            }));
        });

        // Change ONLY the llm model name (the case from the bug report).
        const modelInput = document.querySelector('input[list="available-models-list"]') as HTMLInputElement;
        expect(modelInput).not.toBeNull();
        fireEvent.change(modelInput, { target: { value: 'qwen3-coder:32b' } });

        // Trigger save.
        const saveButton = screen.getByText('Save & Sync to Orchestrator');
        fireEvent.click(saveButton);

        // The updateSetting call should contain the intact compaction block
        // AND the undocumented `experimental` key, in addition to the new llm value.
        const updateSettingCall = mockPostMessage.mock.calls.find(
            args => args && args[0] && args[0].command === 'updateSetting'
        );
        expect(updateSettingCall).toBeDefined();

        const profileSettings = (updateSettingCall![0] as any).config.profile_settings;

        // New value present:
        expect(profileSettings.llm.model_name).toBe('qwen3-coder:32b');

        // Compaction block survived the save round-trip:
        expect(profileSettings.compaction).toBeDefined();
        expect(profileSettings.compaction.model_name).toBe('qwen2.5:1.5b');
        expect(profileSettings.compaction.endpoint_url).toBe('http://127.0.0.1:11434/v1/chat/completions');

        // Undocumented key survived the save round-trip:
        expect(profileSettings.experimental).toBeDefined();
        expect(profileSettings.experimental.speculative_decoding).toBe(true);
    });
});
