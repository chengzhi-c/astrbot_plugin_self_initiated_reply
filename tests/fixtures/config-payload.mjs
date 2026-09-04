// Shared /config payload shape for the settings page. Single source of truth
// for the field set: browser tests and the README screenshot generator both
// build on this so a config key rename can't silently drift the two apart.
// Callers pass `overrides` for scenario-specific values.
export function configPayload(overrides = {}) {
  return {
    ok: true,
    enabled: true,
    runtime_enabled: true,
    whitelist_sessions: ["123456"],
    decision_model_enabled: true,
    decision_prompt_template: "请根据 {latest_message} 判断是否回复",
    decision_prompt_default: "请根据 {latest_message} 判断是否回复",
    config_revision: `sha256:${"b".repeat(64)}`,
    enabled_private_sessions: true,
    abandon_stale_on_new_message: false,
    judge_provider_id: "",
    message_delay_sec: 60,
    min_silence_sec: 45,
    cooldown_sec: 900,
    decision_history_min_messages: 5,
    decision_temperature: 0.2,
    decision_timeout_sec: 20,
    proactive_inherit_tools: false,
    vision_judge_enabled: false,
    vision_main_enabled: false,
    vision_provider_id: "",
    vision_judge_provider_id: "",
    vision_skip_stickers: false,
    vision_max_images: 2,
    vision_image_age_sec: 300,
    vision_timeout_sec: 20,
    ...overrides,
  };
}
