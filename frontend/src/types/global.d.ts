declare class AudioWorkletProcessor {
  constructor(options?: AudioWorkletNodeOptions)
  process(inputs: Float32Array[][], outputs: Float32Array[][], parameters: Record<string, Float32Array>): boolean
  readonly port: MessagePort
}

declare var registerProcessor: (name: string, processorCtor: new (options: AudioWorkletNodeOptions) => AudioWorkletProcessor) => void