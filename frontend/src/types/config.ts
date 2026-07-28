export interface LLMConfig {
  api_key: string
  base_url: string
  model_name: string
}

export interface LLMConfigPublic {
  base_url: string
  model_name: string
  is_configured: boolean
}

export interface ConfigTestResult {
  success: boolean
  message: string
}
