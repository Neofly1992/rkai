// Build against the pinned official header, avoiding duplicated RKLLM struct layouts.
#include <cstddef>
#include <cstdint>
#include "rkllm.h"

using Sink = int (*)(const char*, int, void*);
struct Context {
    LLMHandle handle = nullptr;
    RKLLMCallback callbacks{};
    Sink sink = nullptr;
    void* userdata = nullptr;
};
static int result(RKLLMResult* value, void* userdata, LLMCallState state) {
    auto* context = static_cast<Context*>(userdata);
    return context->sink ? context->sink(value ? value->text : nullptr,
                                        static_cast<int>(state), context->userdata) : 0;
}
extern "C" int rkai_llm_open(const char* path, int context_length, void** output) {
    auto* context = new Context();
    auto parameters = rkllm_createDefaultParam();
    parameters.model_path = path;
    parameters.max_context_len = context_length;
    parameters.is_async = false;
    // Prevent SDK context shifting from silently discarding session history.
    parameters.n_keep = context_length;
    context->callbacks.result_callback = result;
    context->callbacks.result_userdata = context;
    int code = rkllm_init(&context->handle, &parameters, &context->callbacks);
    if (code) { delete context; return code; }
    code = rkllm_set_chat_template(context->handle, "", "", "");
    if (code) { rkllm_destroy(context->handle); delete context; return code; }
    *output = context;
    return 0;
}
extern "C" int rkai_llm_run(void* opaque, const char* prompt, int max_tokens,
                             float temperature, Sink sink, void* userdata) {
    auto* context = static_cast<Context*>(opaque);
    context->sink = sink;
    context->userdata = userdata;
    int code = rkllm_clear_kv_cache(context->handle, 0, nullptr, nullptr);
    if (code) return code;
    RKLLMInput input{};
    input.role = "user";
    input.input_type = RKLLM_INPUT_PROMPT;
    input.prompt_input = prompt;
    RKLLMSamplingParam sampling{};
    const auto defaults = rkllm_createDefaultParam();
    // Greedy at zero temperature; otherwise preserve the official sampler defaults.
    sampling.top_k = temperature == 0.0f ? 1 : defaults.top_k;
    sampling.top_p = defaults.top_p;
    sampling.temperature = temperature;
    sampling.repeat_penalty = defaults.repeat_penalty;
    sampling.frequency_penalty = defaults.frequency_penalty;
    sampling.presence_penalty = defaults.presence_penalty;
    sampling.mirostat = defaults.mirostat;
    sampling.mirostat_tau = defaults.mirostat_tau;
    sampling.mirostat_eta = defaults.mirostat_eta;
    RKLLMInferParam parameters{};
    parameters.mode = RKLLM_INFER_GENERATE;
    parameters.keep_history = 0;
    parameters.max_new_tokens = max_tokens;
    parameters.sampling_params = &sampling;
    code = rkllm_run(context->handle, &input, &parameters, context);
    context->sink = nullptr;
    return code;
}
extern "C" int rkai_llm_abort(void* opaque) {
    return rkllm_abort(static_cast<Context*>(opaque)->handle);
}
extern "C" int rkai_llm_close(void* opaque) {
    auto* context = static_cast<Context*>(opaque);
    int code = rkllm_destroy(context->handle);
    delete context;
    return code;
}
