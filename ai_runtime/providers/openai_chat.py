from ai_runtime.dependencies import *
from ai_runtime.config import _is_gpt5_model, safe_log_headers, _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import save_content_to_db

async def call_o1_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None,
                      input_token_fallback=None,
                      pdf_error_metadata=None,
                      prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                      llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                      pending_attachment_refs: Optional[list[str]] = None):
    global stop_signals
    logger.debug("enters call_o1_api")

    user_id = current_user.id
    error_yielded = False

    # Use user's API key if provided
    api_key_to_use = user_api_key or openai.api_key

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key_to_use}"
    }

    # Prepare messages with prompt first
    api_messages = [{"role": "user", "content": prompt}]

    # Add message history
    for msg in messages:
        if msg['role'] != 'system':  # Avoid duplicating system message
            api_messages.append(msg)

    data = {
        "model": model,
        "messages": api_messages
        # "o1" doesn't support 'stream' parameter
    }

    content = ""
    input_tokens = output_tokens = total_tokens = 0
    reasoning_tokens = 0

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=data) as response:
                if response.status == 200:
                    response_json = await response.json()
                    logger.debug(f"call_o1_api -> response keys: {list(response_json.keys())}")

                    # Extract assistant response
                    if 'choices' in response_json and response_json['choices']:
                        assistant_message = response_json['choices'][0]['message']['content']
                        content = assistant_message

                        # Simulate streaming by splitting response into sentences
                        sentences = re.split('(?<=[.!?]) +', content)
                        for sentence in sentences:
                            if stop_signals.get(conversation_id):
                                logger.info("Stop signal received, exiting o1 API call loop.")
                                break
                            yield f"data: {orjson.dumps({'content': sentence.strip()}).decode()}\n\n"
                            await asyncio.sleep(0.1)  # Small pause to simulate streaming

                        # Extract token usage
                        usage = response_json.get('usage', {})
                        input_tokens = usage.get('prompt_tokens', 0)
                        output_tokens = usage.get('completion_tokens', 0)
                        total_tokens = usage.get('total_tokens', 0)
                        reasoning_tokens = usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)

                    else:
                        logger.error("[call_o1_api] - OpenAI (o1) response had no choices array")
                        yield f"data: {orjson.dumps({'error': 'OpenAI (o1) returned an empty response. Please try again.'}).decode()}\n\n"
                        error_yielded = True
                else:
                    error_body = await response.text()
                    raw_log = f"[call_o1_api] - Error: Received status code {response.status}. Response body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, "OpenAI (o1)")
                    yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (o1)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True
        except asyncio.TimeoutError as exc:
            error_msg = f"[call_o1_api] - Request timed out for conversation {conversation_id}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True
        except aiohttp.ClientError as exc:
            error_msg = f"[call_o1_api] - Connection error: {str(exc)}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True
        except Exception as exc:
            error_msg = f"[call_o1_api] - Unexpected error: {str(exc)}"
            logger.error(error_msg)
            human_msg = _human_exception_error(exc, "OpenAI (o1)")
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True

    # Include reasoning_tokens in output_tokens and total_tokens
    output_tokens += reasoning_tokens
    total_tokens += reasoning_tokens

    # Save the content to the database using read-write connection
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {user_id}. "
                               f"Provider: o1. Not saving to DB.")
                if not error_yielded:
                    yield f'data: {orjson.dumps({"error": "The AI returned an empty response. Please try again."}).decode()}\n\n'
            return
        else:
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, user_id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, byok=byok, pending_attachment_refs=pending_attachment_refs)
            if user_message_id and bot_message_id:
                yield f"data: {orjson.dumps({'message_ids': {'user': user_message_id, 'bot': bot_message_id}}).decode()}\n\n"

        yield content.strip()
    else:
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"


async def call_llm_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, api_url, api_key, provider_label, user_message=None, extra_headers=None, custom_timeout=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False, api_model=None,
                       pending_attachment_refs: Optional[list[str]] = None):
    """
    Generic LLM API call function for OpenAI-compatible APIs.
    Used by GPT, xAI, and OpenRouter.

    Args:
        provider_label: Human-readable provider name for user-facing SSE errors.
        extra_headers: Additional headers to include (e.g., for OpenRouter)
        custom_timeout: Override the default timeout in seconds
        tools: List of tools in OpenAI format (optional). When provided,
               the model can decide to call a tool instead of responding.
    """
    global stop_signals
    logger.info("enters call_llm_api")

    user_id = current_user.id
    error_yielded = False

    messages.insert(0, {"role": "system", "content": prompt})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Merge extra headers if provided (for OpenRouter)
    if extra_headers:
        headers.update(extra_headers)

    # GPT-5+ models require max_completion_tokens instead of max_tokens
    # and don't support custom temperature values (only default 1.0)
    if _is_gpt5_model(model):
        data = {
            "model": api_model or model,
            "max_completion_tokens": max_tokens,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    else:
        data = {
            "model": api_model or model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    # Shallow copy to avoid mutating the caller's list if server tools are appended later
    if tools:
        data["tools"] = list(tools)
        data["tool_choice"] = "auto"  # Let the model decide when to use tools

    content, function_name, function_arguments = "", "", ""
    tool_call_id = ""  # For tracking tool_calls
    input_tokens = output_tokens = total_tokens = 0
    truncated = False

    logger.debug(f"call_llm_api -> messages: {messages}")

    # Configure timeout: use custom_timeout if provided, otherwise check for reasoning models
    if custom_timeout:
        timeout_seconds = custom_timeout
    elif "grok" in model.lower():
        timeout_seconds = 300  # 5 minutes for Grok reasoning models
    else:
        timeout_seconds = 120  # Default 2 minutes
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    # JSON buffer for handling incomplete chunks
                    json_buffer = ""
                    input_tokens = output_tokens = total_tokens = 0

                    async for chunk in response.content.iter_chunked(1024):
                        if stop_signals.get(conversation_id):
                            logger.info("Stop signal received, exiting LLM API call loop.")
                            break

                        chunk_str = chunk.decode("utf-8")
                        json_buffer += chunk_str

                        # Process complete lines from buffer
                        while "\n\n" in json_buffer:
                            line_data, json_buffer = json_buffer.split("\n\n", 1)

                            for line in line_data.split("\n"):
                                line = line.strip()

                                if line.startswith("data: "):
                                    data_part = line[6:]  # Remove 'data: ' prefix

                                    if data_part == "[DONE]":
                                        break

                                    if data_part.startswith("{"):
                                        try:
                                            chunk_data = orjson.loads(data_part)

                                            if 'choices' in chunk_data and chunk_data['choices']:
                                                for choice in chunk_data['choices']:
                                                    if not choice:
                                                        continue
                                                    if 'delta' in choice and choice['delta'] is not None:
                                                        delta = choice['delta']

                                                        # Handle tool_calls (new OpenAI format)
                                                        if 'tool_calls' in delta:
                                                            for tc in delta['tool_calls']:
                                                                if tc.get('id'):
                                                                    tool_call_id = tc['id']
                                                                if tc.get('function'):
                                                                    fn = tc['function']
                                                                    if fn.get('name'):
                                                                        function_name = fn['name']
                                                                        function_arguments = ""
                                                                    if fn.get('arguments'):
                                                                        function_arguments += fn['arguments']

                                                        # Handle function_call (deprecated but still supported)
                                                        elif 'function_call' in delta:
                                                            function_chunk = delta['function_call']
                                                            if function_chunk is not None:
                                                                if 'name' in function_chunk:
                                                                    function_name = function_chunk['name']
                                                                    function_arguments = ""
                                                                elif 'arguments' in function_chunk:
                                                                    function_arguments += function_chunk['arguments']

                                                        # Handle content
                                                        elif 'content' in delta:
                                                            content_chunk = delta['content']
                                                            if content_chunk is not None:
                                                                content += content_chunk
                                                                yield f"data: {orjson.dumps({'content': content_chunk}).decode()}\n\n"

                                                    # Check finish_reason for tool_calls
                                                    finish_reason = choice.get('finish_reason')
                                                    if finish_reason == 'tool_calls' or finish_reason == 'function_call':
                                                        # Tool call completed - will be processed after loop
                                                        continue
                                                    elif finish_reason == 'stop':
                                                        continue
                                                    elif finish_reason in {'length', 'max_tokens', 'max_completion_tokens'}:
                                                        if not truncated:
                                                            truncated = True
                                                            _log_truncated_response(
                                                                provider_label,
                                                                model,
                                                                conversation_id,
                                                                llm_id,
                                                                finish_reason,
                                                                max_tokens,
                                                            )

                                            # Handle usage information
                                            if 'usage' in chunk_data and chunk_data['usage'] and 'total_tokens' in chunk_data['usage']:
                                                input_tokens = chunk_data['usage']['prompt_tokens']
                                                output_tokens = chunk_data['usage']['completion_tokens']
                                                total_tokens = chunk_data['usage']['total_tokens']

                                        except orjson.JSONDecodeError as e:
                                            # Log JSON errors but don't stop processing for Grok reasoning models
                                            if "grok" in model.lower():
                                                logger.warning(f"JSON decode warning for {model}: {e}")
                                            else:
                                                logger.error(f"[call_llm_api] - Error decoding JSON fragment: {e} , data: {data_part[:200]}...")
                else:
                    error_body = await response.text()
                    raw_log = f"[call_llm_api] - Error: Received status code {response.status}. Response body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, provider_label)
                    yield f"data: {orjson.dumps(_provider_error_payload(provider_label, human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True

                    logger.error(f"Request details: URL: {api_url}, Headers: {safe_log_headers(headers)}, "
                                 f"model={data.get('model', '?')}, messages={len(data.get('messages', []))}, "
                                 f"conversation_id={conversation_id}")

                    try:
                        error_json = await response.json()
                        if 'error' in error_json:
                            logger.error(f"API Error details: {error_json['error']}")
                    except:
                        logger.error("Could not parse error response as JSON")

        except asyncio.TimeoutError as exc:
            error_message = f"[call_llm_api] - Request timed out after {timeout_seconds} seconds for model {model}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True

        except aiohttp.ClientError as exc:
            error_message = f"[call_llm_api] - Network error occurred: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True

        except Exception as exc:
            error_message = f"[call_llm_api] - Unexpected error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, provider_label)
            yield f"data: {orjson.dumps({'error': human_msg}).decode()}\n\n"
            error_yielded = True

    # If a tool call was detected, emit it and return without saving to DB
    # The caller (get_ai_response) will handle the tool call and save the result
    # When save_to_db=False (Multi-AI), skip tool handling entirely
    if function_name and save_to_db:
        try:
            # Parse the accumulated arguments as JSON
            parsed_args = orjson.loads(function_arguments) if function_arguments else {}
        except orjson.JSONDecodeError:
            logger.error(f"[call_llm_api] - Failed to parse tool arguments: {function_arguments}")
            parsed_args = {}

        logger.info(f"[call_llm_api] - Tool call detected: {function_name}")
        logger.debug(f"[call_llm_api] - Tool call args: {parsed_args}")

        yield f"data: {orjson.dumps({'tool_call': {'name': function_name, 'arguments': parsed_args, 'id': tool_call_id}}).decode()}\n\n"
        yield f"data: {orjson.dumps({'tool_call_pending': True}).decode()}\n\n"
        return  # Don't save to DB - handler will do it

    # Normal response - save to database
    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {current_user.id}. "
                               f"Provider: llm_api. Not saving to DB.")
                if not error_yielded:
                    yield f'data: {orjson.dumps({"error": "The AI returned an empty response. Please try again."}).decode()}\n\n'
            return
        else:
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, current_user.id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, byok=byok, pending_attachment_refs=pending_attachment_refs)
            if user_message_id and bot_message_id:
                yield f"data: {orjson.dumps({'message_ids': {'user': user_message_id, 'bot': bot_message_id}}).decode()}\n\n"

        yield content.strip()
    else:
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"

async def call_gpt_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                       pending_attachment_refs: Optional[list[str]] = None):
    api_url = "https://api.openai.com/v1/chat/completions"
    api_key = user_api_key or openai.api_key  # Use user's key if provided

    async for chunk in call_llm_api(
        messages,
        model,
        temperature,
        max_tokens,
        prompt,
        conversation_id,
        current_user,
        request,
        api_url,
        api_key,
        "OpenAI (GPT)",
        user_message=user_message,
        input_token_fallback=input_token_fallback,
        pdf_error_metadata=pdf_error_metadata,
        tools=tools,
        prompt_id=prompt_id,
        watchdog_config=watchdog_config,
        watchdog_hint_active=watchdog_hint_active,
        watchdog_hint_eval_id=watchdog_hint_eval_id,
        llm_id=llm_id,
        save_to_db=save_to_db,
        web_search_mode=web_search_mode,
        byok=byok,
        pending_attachment_refs=pending_attachment_refs,
    ):
        yield chunk
