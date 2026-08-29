import React, { useState, useEffect } from 'react';

// Safely acquire and cache the VS Code API
declare const acquireVsCodeApi: any;
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

const TOKEN_PRESETS = [4000, 6000, 8000, 16000, 24000, 32000, 64000, 128000];
const DEFAULT_OLLAMA_PATH = '~/.ollama/models/manifests/registry.ollama.ai/library';

export const SettingsPanel: React.FC = () => {
    const [isLoading, setIsLoading] = useState(true);
    const [rawConfig, setRawConfig] = useState<any>(null);
    
    // Profile & LLM Settings
    const [activeProfile, setActiveProfile] = useState('home');
    const [endpointUrl, setEndpointUrl] = useState('');
    const [apiKey, setApiKey] = useState('');
    const [modelName, setModelName] = useState(''); 
    const [modelsPath, setModelsPath] = useState(DEFAULT_OLLAMA_PATH);
    const [tokenLimit, setTokenLimit] = useState(6000);
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [modelSpecs, setModelSpecs] = useState<Record<string, { recommendedMax: number }>>({});

    // Helper to populate form fields from a specific profile object
    const applyProfileState = (profileName: string, config: any) => {
        const profile = config?.profiles?.[profileName];
        if (profile) {
            setEndpointUrl(profile.llm?.endpoint_url || profile.llm?.endpoint || '');
            setApiKey(profile.llm?.api_key || profile.llm?.apiKey || '');
            setModelName(profile.llm?.model_name || profile.llm?.model || '');
            setModelsPath(profile.llm?.models_path || DEFAULT_OLLAMA_PATH);
            setTokenLimit(profile.memory?.max_tokens || 6000);
        } else {
            if (profileName === 'work') {
                setEndpointUrl('https://your-enterprise-endpoint.azure.com/v1/chat/completions');
                setApiKey('');
                setModelName('gpt-4-turbo');
                setModelsPath('');
                setTokenLimit(8000);
            } else {
                setEndpointUrl('http://127.0.0.1:11434/v1/chat/completions');
                setApiKey('none');
                setModelName('llama3:8b');
                setModelsPath(DEFAULT_OLLAMA_PATH);
                setTokenLimit(6000);
            }
        }
    };

    useEffect(() => {
        vscode.postMessage({ command: 'ready' });

        const handleMessage = (event: MessageEvent) => {
            const message = event.data;
            
            if (message.command === 'loadModels') {
                setAvailableModels(message.models || []);
                if (message.specs) setModelSpecs(message.specs);
            } 
            else if (message.command === 'loadConfig') {
                const config = message.config;
                if (config) {
                    setRawConfig(config);
                    const currentProfile = config.active_profile || 'home';
                    setActiveProfile(currentProfile);
                    applyProfileState(currentProfile, config);
                }
                setIsLoading(false);
            }
        };

        window.addEventListener('message', handleMessage);
        return () => window.removeEventListener('message', handleMessage);
    }, []);

    const handleProfileChange = (newProfile: string) => {
        setActiveProfile(newProfile);
        if (rawConfig) {
            applyProfileState(newProfile, rawConfig);
        }
    };

    const handleModelChange = (val: string) => {
        setModelName(val);
        const matchedKey = Object.keys(modelSpecs).find(key => val.includes(key));
        if (matchedKey && modelSpecs[matchedKey]?.recommendedMax) {
            setTokenLimit(modelSpecs[matchedKey].recommendedMax);
        }
    };

    const handleScanModels = () => {
        vscode.postMessage({ command: 'scanModelsFromPath', path: modelsPath });
    };

    const handleSync = () => {
        vscode.postMessage({
            command: 'updateSetting',
            config: {
                active_profile: activeProfile,
                profile_settings: {
                    llm: { 
                        endpoint_url: endpointUrl,
                        api_key: apiKey,
                        model_name: modelName,
                        models_path: modelsPath 
                    },
                    memory: { 
                        max_tokens: tokenLimit 
                    }
                }
            }
        });
    };

    if (isLoading) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', gap: '1rem', color: 'var(--vscode-foreground)' }}>
                <div style={{ border: '4px solid var(--vscode-widget-border)', borderTop: '4px solid var(--vscode-button-background)', borderRadius: '50%', width: '40px', height: '40px', animation: 'spin 1s linear infinite' }}></div>
                <style>{`@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
                <h3>Fetching Workspace Environment...</h3>
            </div>
        );
    }

    const inputStyle: React.CSSProperties = {
        padding: '0.5rem',
        marginTop: '0.25rem',
        backgroundColor: 'var(--vscode-input-background)',
        color: 'var(--vscode-input-foreground)',
        border: '1px solid var(--vscode-input-border)'
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', padding: '1rem' }}>
            <h2>Workspace Environment</h2>
            
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Active Profile</label>
                <select value={activeProfile} onChange={(e) => handleProfileChange(e.target.value)} style={inputStyle}>
                    <option value="home">🏠 Home (Local Ollama)</option>
                    <option value="work">🏢 Work (Azure Foundry / Cloud)</option>
                </select>
            </div>

            <hr style={{ width: '100%', borderColor: 'var(--vscode-widget-border)' }} />
            
            <h3>{activeProfile.toUpperCase()} Profile Settings</h3>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Endpoint URL</label>
                <input type="text" value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} style={inputStyle} />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>API Key / Auth Token</label>
                <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} style={inputStyle} />
            </div>

            {/* NEW: Models Directory Path */}
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Local Models Path (For Dropdown)</label>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                    <input 
                        type="text"
                        value={modelsPath}
                        onChange={(e) => setModelsPath(e.target.value)}
                        placeholder="e.g. ~/.ollama/models/manifests/registry.ollama.ai/library"
                        style={{ ...inputStyle, flexGrow: 1 }}
                    />
                    <button 
                        onClick={handleScanModels}
                        style={{
                            padding: '0.5rem 1rem',
                            marginTop: '0.25rem',
                            backgroundColor: 'var(--vscode-button-secondaryBackground)',
                            color: 'var(--vscode-button-secondaryForeground)',
                            border: 'none',
                            cursor: 'pointer'
                        }}
                    >
                        Scan Directory
                    </button>
                </div>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Model Deployment Name</label>
                <input type="text" list="available-models-list" value={modelName} onChange={(e) => handleModelChange(e.target.value)} style={inputStyle} />
                <datalist id="available-models-list">
                    {availableModels.map(model => (
                        <option key={model} value={model} />
                    ))}
                </datalist>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Max Context Tokens</label>
                <input type="number" list="token-presets-list" value={tokenLimit} onChange={(e) => setTokenLimit(Number(e.target.value))} style={inputStyle} />
                <datalist id="token-presets-list">
                    {TOKEN_PRESETS.map(preset => (
                        <option key={preset} value={preset} />
                    ))}
                </datalist>
            </div>

            <button onClick={handleSync} style={{ padding: '0.6rem 1rem', cursor: 'pointer', marginTop: '0.5rem', backgroundColor: 'var(--vscode-button-background)', color: 'var(--vscode-button-foreground)', border: 'none', fontWeight: 'bold' }}>
                Save & Sync to Orchestrator
            </button>
        </div>
    );
};
