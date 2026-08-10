//! PTZ value types shared by the read-only capability report and PTZ
//! movement control. They live in their own module so neither
//! `capabilities.rs` nor `ptz.rs` has to import the other; each re-exports
//! (or uses directly) what it needs from here instead.

use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct PtzSpaces {
    pub absolute_pan_tilt: bool,
    pub absolute_zoom: bool,
    pub relative_pan_tilt: bool,
    pub relative_zoom: bool,
    pub continuous_pan_tilt: bool,
    pub continuous_zoom: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PtzNode {
    pub token: String,
    pub spaces: PtzSpaces,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub maximum_presets: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub home_supported: Option<bool>,
    pub auxiliary_commands: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct PtzServiceCapabilities {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub e_flip: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reverse: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub get_compatible_configurations: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub move_status: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status_position: Option<bool>,
}
