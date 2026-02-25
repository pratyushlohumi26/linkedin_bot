
import json
import time
from openai import OpenAI
import anthropic
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import time
import tweepy
from telebot import TeleBot
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import re
import requests
import json
import os
import ast
from prompts import system_prompt_linkedin, system_prompt_x
from api_key import x_access_token, x_api_key, x_bearer_token, x_client_id, x_client_secret, x_api_secret_key, x_access_token_secret
from api_key import TELEGRAM_TOKEN
from api_key import LINKEDIN_TOKEN
from api_key import openai_key, anthropic_api_key


os.environ['OPENAI_API_KEY'] =  openai_key
client_openai = OpenAI(api_key=openai_key)
client_claude = anthropic.Anthropic(
        # defaults to os.environ.get("ANTHROPIC_API_KEY")
        api_key=anthropic_api_key,
    )

bot = TeleBot(TELEGRAM_TOKEN)

user_info = {}

class LinkedinAutomate:
    def __init__(self, access_token=LINKEDIN_TOKEN, yt_url='', title='', description=''):
        self.access_token = access_token
        self.yt_url = yt_url
        self.title = title
        self.description = description
        self.python_group_list = [762547,961087,45655,1814785]
        self.headers = {
            'Authorization': f'Bearer {self.access_token}'
        }

    def common_api_call_part(self, feed_type = "feed", group_id = None):
        payload_dict_no_media = {
            "author": f"urn:li:person:{self.user_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": self.description
                    },
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            }
        }
        return json.dumps(payload_dict_no_media)

    def extract_thumbnail_url_from_YT_video_url(self):
#        exp = "^.*((youtu.be\/)|(v\/)|(\/u\/\w\/)|(embed\/)|(watch\?))\??v?=?([^#&?]*).*"
        s = re.findall(exp,self.yt_url)[0][-1]
        return  f"https://i.ytimg.com/vi/{s}/maxresdefault.jpg"

    def get_user_id(self):
        url = "https://api.linkedin.com/v2/userinfo"
        response = requests.request("GET", url, headers=self.headers)
        jsonData = json.loads(response.text)
        # print("--> Data from Linkedin", jsonData)
        return jsonData["sub"]

    def feed_post(self):
        url = "https://api.linkedin.com/v2/ugcPosts"
        payload = self.common_api_call_part()

        return requests.request("POST", url, headers=self.headers, data=payload)

    def group_post(self, group_id):
        url = "https://api.linkedin.com/v2/ugcPosts"
        payload = self.common_api_call_part(feed_type = "group", group_id=group_id)

        return requests.request("POST", url, headers=self.headers, data=payload)


    def main_func(self):
        self.user_id = self.get_user_id()
        # print(self.user_id)

        feed_post = self.feed_post()
        return feed_post

# Function to fetch and extract text from a URL

def extract_text_from_url(url):
    try:
        # Send a GET request to the URL
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad status codes

        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')

        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()

        # Get text
        text = soup.get_text()

        # Break into lines and remove leading and trailing space on each
        lines = (line.strip() for line in text.splitlines())

        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))

        # Drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)

        return text

    except requests.RequestException as e:
        print(f"Error fetching the URL: {e}")
        return None
# Function to summarize text using Claude
def summarize_with_claude_linkedin(text):
    # client = anthropic.client_claude(api_key=os.getenv('ANTHROPIC_API_KEY'))

    

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
              ]
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
      ]
  )
  # print(message.content)
  str_dict = message.content[0].text
  print("---> Output received from the model:: \n\n", str_dict)
  try:
    python_dict = eval(str_dict)#ast.literal_eval(str_list)
    return python_dict
  except Exception as e:
    return e

def summarize_with_gpt4_linkedin(text):
    # client = anthropic.client_claude(api_key=os.getenv('ANTHROPIC_API_KEY'))
    try:
        response = client_openai.chat.completions.create(
                    model="gpt-4.1",
                    messages=[
                    {
                    "role": "system",
                    "content": [
                        {
                        "text": system_prompt_linkedin,
                        "type": "text"
                        }
                    ]
                    },
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
                temperature=1,
                max_tokens=2048,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
                response_format={
                    "type": "text"
                }
                )
        answer = response.choices[0].message.content
        # Remove asterisks using replace() method
        cleaned_text = answer.replace('*', '')

        # print(cleaned_text)
        return cleaned_text
    except Exception as e:
        print(f"Error generating summary: {e}")
        return None

def call_gpt4_x(blog_text):
  
    response = client_openai.chat.completions.create(
                    model="gpt-4.1",
                    messages=[
                    {
                    "role": "system",
                    "content": [
                        {
                        "text": system_prompt_x,
                        "type": "text"
                        }
                    ]
                    },
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
                temperature=1,
                max_tokens=2048,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
                response_format={
                    "type": "text"
                }
                )
    answer = response.choices[0].message.content
    # print(message.content)
    str_dict = answer
    print("---> Output received from the model:: \n\n", str_dict)
    try:
        python_dict = eval(str_dict)#ast.literal_eval(str_list)
        return python_dict
    except Exception as e:
        return e

def post_twitter(tweet_thread={1:'Nothing',2:'is',3:'True'}):
  client = tweepy.Client(consumer_key=x_api_key,consumer_secret=x_api_secret_key,access_token=x_access_token,access_token_secret=x_access_token_secret)
  for i,tweet in tweet_thread.items():
    thread = f"[{i}/{len(tweet_thread)}] "+ tweet
    print("--> ",thread)
    response = client.create_tweet(text=thread)
    time.sleep(0.5)
  try:
    id = response.data['id']
    twitter_link =f'https://x.com/PratyushLohumi/status/{id}'
    return twitter_link
  except Exception as e:
    return e

def feed_type_selection():
    markup = InlineKeyboardMarkup()
    markup.row_width = 3
    markup.add(
        InlineKeyboardButton("Twitter Thread", callback_data="twitter"),
        InlineKeyboardButton("Linkedin Post", callback_data="linkedin"),
        InlineKeyboardButton("Both", callback_data="both")
    )
    return markup

def confirmation_selection():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(InlineKeyboardButton("Yes", callback_data="yes"),InlineKeyboardButton("No", callback_data="no"))
    return markup

# Idea for Description of Video
# Start with, this post is automated.....
@bot.message_handler(commands=['help', 'start'])
def send_welcome(message):
    bot.send_message(message.chat.id, """\
/help and /start to get the help
/start_post to post in social Media
""")


@bot.message_handler(commands=['start_post'])
def linkedin_post_handler(message):
    bot.send_message(message.chat.id, "Select which type of Post you want to make", reply_markup=feed_type_selection())

@bot.message_handler(func=lambda message: True)
def echo_message(message):
    bot.reply_to(message, message.text)

def process_text_post(message):
    text = message.text
    blog_text = extract_text_from_url(text)
    user_info["description"] = blog_text

    bot.send_message(message.chat.id, \
f"""
    This is what the scrapped text looks like ::
<b>{blog_text[:100]}</b>
"""\
,reply_markup=confirmation_selection(), parse_mode="html")

# @bot.callback_query_handler(func=lambda call: True)
@bot.callback_query_handler(lambda query: query.data in ["twitter", "linkedin", "both"])
def post_type_callback_handler(call):
    if call.data == "linkedin":
        user_info["feed_type"] = 'linkedin'
    elif call.data == "twitter":
        user_info["feed_type"] = 'twitter'
    elif call.data == "both":
        user_info["feed_type"] = 'both'
    else:
        bot.send_message(call.message, "Something went wrong")

    msg = bot.reply_to(call.message, 'Whats the link which needs to be posted?')
    # print("----> Got this id", call.message, call)
    bot.register_next_step_handler(msg, process_text_post)
        # msg = bot.reply_to(yt_url_msg, 'Great, now please enter the description')
        # bot.register_next_step_handler(msg, process_text_post, "description")

@bot.callback_query_handler(lambda query: query.data in ["yes", "no"])
def confirmation_callback_handler(call):
    if call.data == "yes":
        description = user_info["description"]
        post_type = user_info["feed_type"]
        # post_media_category = POST_TYPE_TEXT

        if post_type=='linkedin':
        #   summary = summarize_with_claude_linkedin(description)
          summary = summarize_with_gpt4_linkedin(description)
          msg = bot.reply_to(call.message, f'Posting this msg......... {summary}')
          linkedin_response = LinkedinAutomate(description=summary).main_func()
          print("Response got from Linkedin -- ", linkedin_response)
          # print(type(post_response))
          if linkedin_response.status_code == 201:
          # url_of_post = f"{BASE_LINKEDIN_URL_FOR_POST}{post_respose.headers.get('x-linkedin-id')}"
            # bot.reply_to(call.message, f'Posteddddd, successfully - {post_response}')

            bot.reply_to(call.message, f'Linkedin Post Link \n\n https://www.linkedin.com/in/pratyush-lohumi/recent-activity/all/ : {linkedin_response}', )
            # msg = bot.reply_to(call.message, f'Posteddddd, successfully - {post_response}')
        elif post_type=='twitter':
        #   tweet_thread = call_claude_x(description)
          tweet_thread = call_gpt4_x(description)
          time.sleep(3)
          twitter_response = post_twitter(tweet_thread=tweet_thread)
          print("Twitter Response :", twitter_response)
          # bot.send_message(call.id, f'Link of the twitter thread : {twitter_response}')
        elif post_type=='both':
          # print("------>", call)
            # tweet_thread = call_claude_x(description)
            # summary = summarize_with_claude_linkedin(description)
            tweet_thread = call_gpt4_x(description)
            summary = summarize_with_gpt4_linkedin(description)
            linkedin_response = LinkedinAutomate(description=summary).main_func()
            twitter_response = post_twitter(tweet_thread=tweet_thread)
            print(f"Twitter Response : {twitter_response}\n Linkedin Response : {linkedin_response} https://www.linkedin.com/in/pratyush-lohumi/recent-activity/all/")
            bot.reply_to(call.message, f"Twitter Response : {twitter_response}\n Linkedin Response : {linkedin_response} https://www.linkedin.com/in/pratyush-lohumi/recent-activity/all/", )
            # bot.send_message(call.id, f'Twitter Response : {twitter_response} \n Linkedin Response : {linkedin_response}')
    elif call.data == "no":
        msg = bot.send_message(call.id, "Then what??")
        bot.register_next_step_handler(msg, process_text_post)




# Step 3: Start infinite polling
# Enable saving next step handlers to file "./.handlers-saves/step.save".
# Delay=2 means that after any change in next step handlers (e.g. calling register_next_step_handler())
# saving will hapen after delay 2 seconds.
bot.enable_save_next_step_handlers(delay=2)

# Load next_step_handlers from save file (default "./.handlers-saves/step.save")
# WARNING It will work only if enable_save_next_step_handlers was called!
bot.load_next_step_handlers()
bot.infinity_polling()