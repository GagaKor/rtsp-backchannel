/**
 * PTZ value types shared by the read-only capability report and PTZ control.
 * They live here so neither module has to import the other.
 */
export interface PtzServiceCapabilities {
  eFlip?: boolean;
  reverse?: boolean;
  getCompatibleConfigurations?: boolean;
  moveStatus?: boolean;
  statusPosition?: boolean;
}

export interface PtzSpaces {
  absolutePanTilt: boolean;
  absoluteZoom: boolean;
  relativePanTilt: boolean;
  relativeZoom: boolean;
  continuousPanTilt: boolean;
  continuousZoom: boolean;
}

export interface PtzNode {
  token: string;
  name?: string;
  spaces: PtzSpaces;
  maximumPresets?: number;
  homeSupported?: boolean;
  auxiliaryCommands: string[];
}
