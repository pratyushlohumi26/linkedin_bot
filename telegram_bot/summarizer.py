#!/usr/bin/env python3
"""Summarization helpers for LinkedIn and X via Claude & GPT-4."""

import ast
import logging

from openai import OpenAI
import anthropic

from prompts import system_prompt_linkedin, system_prompt_x
from api_key import openai_key, anthropic_api_key

logger = logging.getLogger(__name__)

client_openai = OpenAI(api_key=openai_key)
client_claude = anthropic.Anthropic(api_key=anthropic_api_key)

def summarize_with_claude_linkedin(text):
    try:
        message = client_claude.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=1000,
            temperature=0,
            system=system_prompt_linkedin,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"""Here is the blogpost text scrapped from the web: {text}
                              Just give the Post text as the response."""
                        }
                    ]
                }
            ],
        )
        return message.content[0].text
    except Exception as e:
        print(f"Error generating summary: {e}")
        return None

def call_claude_x(blog_text):
    message = client_claude.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=1000,
        temperature=0,
        system=system_prompt_x,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Here is the blogpost text scrapped from the web: ```{blog_text}```"
                    }
                ]
            }
        ],
    )
    str_dict = message.content[0].text
    print("---> Output received from the model:: \n\n", str_dict)
    try:
        python_dict = eval(str_dict)
        return python_dict
    except Exception as e:
        return e

def summarize_with_gpt4_linkedin(text):
    try:
        response = client_openai.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": [{"text": system_prompt_linkedin, "type": "text"}]},
                {"role": "user", "content": [{"type": "text", "text": f"""Here is the blogpost text scrapped from the web: {text}
                              Just give the Post text as the response."""}]}],
            temperature=1,
            max_tokens=2048,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            response_format={"type": "text"},
        )
        answer = response.choices[0].message.content
        cleaned_text = answer.replace('*', '')
        return cleaned_text
    except Exception as e:
        print(f"Error generating summary: {e}")
        return None

def call_gpt4_x(blog_text):
    response = client_openai.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": [{"text": system_prompt_x, "type": "text"}]},
            {"role": "user", "content": [{"type": "text", "text": f"Here is the blogpost text scrapped from the web: ```{blog_text}```"}]}],
        temperature=1,
        max_tokens=2048,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0,
        response_format={"type": "text"},
    )
    answer = response.choices[0].message.content
    print("---> Output received from the model:: \n\n", answer)
    try:
        python_dict = eval(answer)
        return python_dict
    except Exception as e:
        return e