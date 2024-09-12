# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import argparse
import asyncio
import base64
import io
import json
import multiprocessing
import os
import queue
import random
import requests
import threading
import time
from PIL import Image
from typing import List, Iterable, Union

import numpy as np
from transformers import AutoTokenizer
from openai import OpenAI, AzureOpenAI
from azure.identity import DefaultAzureCredential
from azure.identity import get_bearer_token_provider

try:
    from .postprocess_results import ResponseDetails
    from .random_query_generator import RandomQueryGenerator
    from .sample_input import all_text
    from .utils import parse_args, print_summary, get_args_product, CLIENT_PARAMS
except ImportError:
    from postprocess_results import ResponseDetails
    from random_query_generator import RandomQueryGenerator
    from sample_input import all_text
    from utils import parse_args, print_summary, get_args_product, CLIENT_PARAMS


def call_fastgen(
    input_tokens: str, max_new_tokens: int, args: argparse.Namespace
) -> ResponseDetails:
    import mii

    client = mii.client(args.deployment_name)

    output_tokens = []
    token_gen_time = []
    time_last_token = 0

    def callback(response):
        nonlocal time_last_token
        # print(f"Received: {response[0].generated_text} time_last_token={time_last_token}")
        output_tokens.append(response[0].generated_text)
        time_now = time.time()
        token_gen_time.append(time_now - time_last_token)
        time_last_token = time_now

    time_last_token = start_time = time.time()
    token_gen_time = []
    if args.stream:
        output_tokens = []
        client.generate(
            input_tokens, max_new_tokens=max_new_tokens, streaming_fn=callback
        )
    else:
        result = client.generate(input_tokens, max_new_tokens=max_new_tokens)
        output_tokens = result[0].generated_text

    return ResponseDetails(
        generated_tokens=output_tokens,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def call_vllm(
    input_tokens: str, max_new_tokens: int, args: argparse.Namespace
) -> ResponseDetails:
    if not args.stream:
        raise NotImplementedError("Not implemented for non-streaming")

    api_url = "http://localhost:26500/generate"
    headers = {"User-Agent": "Benchmark Client"}
    pload = {
        "prompt": input_tokens,
        "n": 1,
        "use_beam_search": False,
        "temperature": 1.0,
        "top_p": 0.9,
        "max_tokens": max_new_tokens,
        "ignore_eos": False,
        "stream": args.stream,
    }

    def clear_line(n: int = 1) -> None:
        LINE_UP = "\033[1A"
        LINE_CLEAR = "\x1b[2K"
        for _ in range(n):
            print(LINE_UP, end=LINE_CLEAR, flush=True)

    def get_streaming_response(
        response: requests.Response, time_last_token
    ) -> Iterable[List[str]]:
        for chunk in response.iter_lines(
            chunk_size=8192, decode_unicode=False, delimiter=b"\0"
        ):
            if chunk:
                data = json.loads(chunk.decode("utf-8"))
                output = data["text"][0]
                time_now = time.time()
                yield output, time_now - time_last_token
                time_last_token = time_now

    # For non-streaming, but currently non-streaming is not fully implemented
    def get_response(response: requests.Response) -> List[str]:
        data = json.loads(response.content)
        output = data["text"]
        return output

    token_gen_time = []
    start_time = time.time()
    response = requests.post(api_url, headers=headers, json=pload, stream=args.stream)
    for h, t in get_streaming_response(response, start_time):
        output = h
        token_gen_time.append(t)

    return ResponseDetails(
        generated_tokens=output,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def call_aml(
    input_tokens: str,
    max_new_tokens: int,
    args: argparse.Namespace,
    start_time: Union[None, float] = None,
) -> ResponseDetails:
    if args.stream:
        raise NotImplementedError("Not implemented for streaming")

    headers = {
        "Content-Type": "application/json",
        "Authorization": ("Bearer " + args.aml_api_key),
        "azureml-model-deployment": args.deployment_name,
    }

    image = Image.open(args.input_image).convert("RGB")
    img_bytes = io.BytesIO()
    image.save(img_bytes, format="JPEG")
    img_str = base64.b64encode(img_bytes.getvalue()).decode("utf-8")

    pload = {
        "input_data": {
            "input_string": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": input_tokens,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}
                        },
                    ],
                }
            ],
            "parameters": {
                "temperature": 0.0,
                "max_tokens": max_new_tokens,
                "return_full_text": False,
            },
        }
    }

    def get_response(response: requests.Response) -> List[str]:
        data = json.loads(response.content)
        try:
            output = data["output"]
        except (KeyError, TypeError):
            try:
                output = data[0]
            except (KeyError, TypeError):
                output = data
        return output

    token_gen_time = []
    response = None
    if start_time is None:
        start_time = time.time()
    while True:
        try: # Sometimes the AML endpoint will return an error, so we send the request again
            response = requests.post(args.aml_api_url, headers=headers, json=pload, timeout=180)
            output = get_response(response)
            break
        except Exception as e:
            print(f"Connection failed with {e}. Retrying AML request")
            # make sure response exist before we call it
            if response:
                print(f"{response.status_code}:{response.content}")

    return ResponseDetails(
        generated_tokens=output,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def call_azure_openai(
    input_tokens: str,
    max_new_tokens: int,
    args: argparse.Namespace,
    start_time: Union[None, float] = None,
) -> ResponseDetails:
    if args.stream:
        raise NotImplementedError("Not implemented for streaming")

    credential = DefaultAzureCredential()
    token_provider= get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

    print(args.aml_api_url)
    print(args.deployment_name)

    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        azure_endpoint=args.aml_api_url,
        api_version="2024-04-01-preview",
        azure_deployment=args.deployment_name,
        max_retries=0
    )
    models = client.models.list()
    model = models.data[0].id

    image = Image.open(args.input_image).convert("RGB")
    img_bytes = io.BytesIO()
    image.save(img_bytes, format="JPEG")
    img_str = base64.b64encode(img_bytes.getvalue()).decode("utf-8")

    token_gen_time = []
    if start_time is None:
        start_time = time.time()
    while True:
        try:
            chat_response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=max_new_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}
                            },
                            {"type": "text", "text": input_tokens}
                        ],
                    }
                ],
            )
            output = chat_response.choices[0].message.content
            break
        except Exception as e:
            print(f"Connection failed with {e}. Retrying OpenAI request")
            # make sure response exist before we call it
            # if response:
            #     print(f"{response.status_code}:{response.content}")

    return ResponseDetails(
        generated_tokens=output,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def call_openai(
    input_tokens: str,
    max_new_tokens: int,
    args: argparse.Namespace,
    start_time: Union[None, float] = None,
) -> ResponseDetails:
    if args.stream:
        raise NotImplementedError("Not implemented for streaming")

    openai_api_key = "EMPTY"
    # openai_api_key = args.aml_api_key
    openai_api_base = args.aml_api_url

    client = OpenAI(
        api_key=openai_api_key,
        base_url=openai_api_base,
    )
    models = client.models.list()
    model = models.data[0].id

    image = Image.open(args.input_image).convert("RGB")
    img_bytes = io.BytesIO()
    image.save(img_bytes, format="JPEG")
    img_str = base64.b64encode(img_bytes.getvalue()).decode("utf-8")

    token_gen_time = []
    if start_time is None:
        start_time = time.time()
    while True:
        try:
            chat_response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=max_new_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}
                            },
                            {"type": "text", "text": input_tokens}
                        ],
                    }
                ],
            )
            output = chat_response.choices[0].message.content
            break
        except Exception as e:
            print(f"Connection failed with {e}. Retrying OpenAI request")
            # make sure response exist before we call it
            # if response:
            #     print(f"{response.status_code}:{response.content}")

    return ResponseDetails(
        generated_tokens=output,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def _run_parallel(
    barrier: Union[threading.Barrier, multiprocessing.Barrier],
    query_queue: Union[queue.Queue, multiprocessing.Queue],
    result_queue: Union[queue.Queue, multiprocessing.Queue],
    args: argparse.Namespace,
):
    pid = os.getpid()
    session_id = f"test_session_p{pid}_t{threading.get_ident()}"

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    backend_call_fns = {"fastgen": call_fastgen, "vllm": call_vllm, "aml": call_aml, "openai": call_azure_openai}
    call_fn = backend_call_fns[args.backend]

    barrier.wait()

    for _ in range(args.warmup):
        print(f"warmup queue size: {query_queue.qsize()} ({pid})", flush=True)
        input_tokens, req_max_new_tokens = query_queue.get(timeout=1.0)
        _ = call_fn(input_tokens, req_max_new_tokens, args)

    barrier.wait()

    time.sleep(random.uniform(0, args.num_clients) * 0.01)
    try:
        while True:
            print(f"queue size: {query_queue.qsize()} ({pid})", flush=True)
            input_tokens, req_max_new_tokens = query_queue.get(timeout=1.0)

            r = call_fn(input_tokens, req_max_new_tokens, args)

            result_queue.put(r)
    except queue.Empty:
        print(f"queue is empty ({pid})")

    print(f"Worker ({pid}) finished. session_id: {session_id}")


def run_client(args):
    """
    Run MII client for benchmarking. The scenario is a bit complicated:
    1. The main process puts `num_requests` queries into the input queue
    2. Each client runs `warmup` iterations () taking the queries from the input queue
    3. --- barrier ---
    4. The main process marks the start time
    5a. All clients send `num_requests' query in total and put the results into the result queue
    5b. The main process takes the results from the result queue (in parallel with 5a)
    6. The main process marks the end time after receiving `num_requests' results
    """

    if args.use_thread:
        runnable_cls = threading.Thread
        barrier_cls = threading.Barrier
        queue_cls = queue.Queue
    else:
        runnable_cls = multiprocessing.Process
        barrier_cls = multiprocessing.Barrier
        queue_cls = multiprocessing.Queue

    barrier = barrier_cls(args.num_clients + 1)
    query_queue = queue_cls()
    result_queue = queue_cls()

    processes = [
        runnable_cls(
            target=_run_parallel,
            args=(
                barrier,
                query_queue,
                result_queue,
                args,
            ),
        )
        for i in range(args.num_clients)
    ]
    for p in processes:
        p.start()

    # tokenizer = AutoTokenizer.from_pretrained(args.model)
    # query_generator = RandomQueryGenerator(all_text, tokenizer, seed=42)
    # request_text = query_generator.get_random_request_text(
    #     args.mean_prompt_length,
    #     args.mean_prompt_length * args.prompt_length_var,
    #     args.max_prompt_length,
    #     args.num_requests + args.warmup * args.num_clients,
    # )

    request_text = [f"Write an essay of at least 600 words according to the text in the image." for _ in range(args.num_requests + args.warmup * args.num_clients)]

    for t in request_text:
        # Set max_new_tokens following normal distribution
        # req_max_new_tokens = int(
        #     np.random.normal(
        #         args.mean_max_new_tokens,
        #         args.max_new_tokens_var * args.mean_max_new_tokens,
        #     )
        # )
        req_max_new_tokens = args.mean_max_new_tokens
        query_queue.put((t, req_max_new_tokens))

    # Tokenizers must be initialized after fork.
    # So we need to fork before putting inputs to the queue.
    # We need this barrier to stop child processse from taking inputs before the main process puts them
    barrier.wait()
    # This barrier is to make sure that all clients have finished warmup
    barrier.wait()

    response_details = []
    while len(response_details) < args.num_requests:
        res = result_queue.get()
        # vLLM returns concatinated tokens
        if args.backend == "vllm":
            all_tokens = tokenizer.tokenize(res.generated_tokens)
            res.generated_tokens = all_tokens[len(tokenizer.tokenize(res.prompt)) :]
        response_details.append(res)

    return response_details


if __name__ == "__main__":
    args = parse_args(client_args=True)

    for client_args in get_args_product(args, which=CLIENT_PARAMS):
        response_details = run_client(client_args)

        print_summary(client_args, response_details)
