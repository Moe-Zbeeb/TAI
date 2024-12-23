import streamlit as st
import PyPDF2
import logging
import tempfile
import os
import base64
import pandas as pd
import plotly.express as px
import matplotlib.pyplot as plt
import csv  
import streamlit as st
from langchain_openai import ChatOpenAI  # Ensure you have the correct package installed
from langchain.schema import HumanMessage, SystemMessage
from sentence_transformers import SentenceTransformer
import urllib.request
from bs4 import BeautifulSoup
from sklearn.metrics.pairwise import cosine_similarity
import re
import os
import numpy as np
from langchain.chat_models import ChatOpenAI
from langchain.schema import Document
from langchain.document_loaders import PyPDFLoader, TextLoader
from langchain.cache import InMemoryCache
from langchain.chains import RetrievalQA
from langchain.chains.summarize import load_summarize_chain
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate    
from langchain.prompts import ChatPromptTemplate
from langchain.text_splitter import CharacterTextSplitter
from langchain.vectorstores import FAISS
from langchain.embeddings import OpenAIEmbeddings
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from database import SessionLocal
from gtts import gTTS  
from models import User, Course, CourseFile, StudentQuestion
from io import BytesIO
from wordcloud import WordCloud, STOPWORDS
from application1 import about_page, contact_page, inject_css, inject_css2, set_overlay_bg_image, encode_image_to_base64
from PIL import Image
import os
import base64
import pandas as pd
import streamlit as st
import re
import csv
from datetime import datetime
import logging
import re
import subprocess
import assemblyai as aai
from dotenv import load_dotenv
load_dotenv()
aai.settings.api_key = "76e966abc56746f88f365735a37c766f"  

def validate_youtube_url(url):
    """Validate if a URL is a valid YouTube video link."""
    pattern = r"^(https?://)?(www\.)?(youtube\.com|youtu\.?be)/.+$"
    return re.match(pattern, url) is not None

def download_audio_yt_dlp(video_url, output_dir):
    """Download audio using yt-dlp with a custom user agent."""
    try:
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, "%(title)s.%(ext)s")
        command = [
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "--user-agent", "Mozilla/5.0",
            "--output", output_file,
            video_url
        ]
        subprocess.run(command, check=True)
        return True
    except Exception as e:
        print(f"Error downloading audio for {video_url}: {e}")
        return False

def process_youtube_links(youtube_links, course, output_dir="transcripts"):
    """Download audio from YouTube links, transcribe them, save transcripts, and add them to the vector DB."""
    os.makedirs(output_dir, exist_ok=True)
    transcriber = aai.Transcriber()
    transcripts = {}

    for link in youtube_links:
        if not validate_youtube_url(link):
            print(f"Invalid YouTube URL: {link}")
            continue

        print(f"Processing: {link}")
        if not download_audio_yt_dlp(link, output_dir):
            continue
       
        try:
            # Find the downloaded MP3 file (assuming one link at a time)
            audio_file = next((f for f in os.listdir(output_dir) if f.endswith(".mp3")), None)
            if audio_file:
                audio_path = os.path.join(output_dir, audio_file)
                config = aai.TranscriptionConfig(speaker_labels=True)
                transcript = transcriber.transcribe(audio_path, config)
               
                # Save transcript to a text file
                transcript_file = os.path.join(output_dir, f"{audio_file}_transcript.txt")
                with open(transcript_file, "w") as f:
                    for utterance in transcript.utterances:
                        f.write(f"Speaker {utterance.speaker}: {utterance.text}\n")
               
                print(f"Transcript saved: {transcript_file}")
               
                # Store transcript in dictionary
                transcript_text = "\n".join(
                    [f"Speaker {utterance.speaker}: {utterance.text}" for utterance in transcript.utterances]
                )
                transcripts[audio_file] = transcript_text
               
                # Save transcript to database
                transcript_filename = f"{os.path.splitext(audio_file)[0]}_transcript.txt"
                existing_file = session_db.query(CourseFile).filter_by(
                    course_id=course.id, filename=transcript_filename
                ).first()

               

                course_file = CourseFile(
                    filename=transcript_filename,
                    data=transcript_text.encode('utf-8'),
                    course_id=course.id
                )
                session_db.add(course_file)
                session_db.commit()
                print(f"Transcript added to course {course.name} as course file.")

                # Add transcript to vector database
                docs = langchain_handler.load_document(transcript_file)
                if docs:
                    # Fetch existing vector store or create a new one
                    vector_store = langchain_handler.create_vector_store(docs)
                    if not vector_store:
                        print(f"Failed to create vector store for {transcript_filename}.")
                        continue
                   
                    # Check if course already has a vector store
                    existing_vector_store = session_db.query(CourseFile).filter_by(course_id=course.id).first()
                    if existing_vector_store:
                        # Merge with existing vector store
                        vector_store.merge(existing_vector_store)
                        print(f"Transcript vectorized and merged into existing vector store for course {course.name}.")
                    else:
                        print(f"Transcript vectorized and added as new vector store for course {course.name}.")
            else:
                print(f"No audio file found for {link}.")
        except Exception as e:
            print(f"Error processing {link}: {e}")

    return transcripts

# ---------------------- Configuration ----------------------

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize in-memory cache
cache = InMemoryCache()

# Initialize database session
session_db = SessionLocal()

# Fetch OpenAI API key from environment variables for security
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    logging.error("OpenAI API key is not set.")
    st.error("OpenAI API key is not set. Please set the OPENAI_API_KEY environment variable.")
    st.stop()

# Initialize LLM
try:
    llm = ChatOpenAI(
        model="gpt-4",
        temperature=0.2,
        openai_api_key=OPENAI_API_KEY,
    )
    logging.info("Successfully connected to OpenAI LLM.")
except Exception as e:
    logging.error(f"Failed to initialize OpenAI LLM: {str(e)}")
    st.error(f"Failed to initialize OpenAI LLM: {str(e)}")
    st.stop()

# ---------------------- LangchainHandler Class ----------------------

class LangchainHandler:
    def __init__(self, llm):
        self.llm = llm

    def load_document(self, file_path):
        """
        Load a document (PDF or text) from the specified file path.
        :param file_path: Path to the document file.
        :return: List of Document objects.
        """
        try:
            logging.info(f"Loading document from: {file_path}")
            if file_path.lower().endswith('.pdf'):
                loader = PyPDFLoader(file_path)
            else:
                loader = TextLoader(file_path)
            raw_docs = loader.load()
            logging.info(f"Loaded {len(raw_docs)} documents.")

            # Split documents into chunks
            text_splitter = CharacterTextSplitter(chunk_size=2000, chunk_overlap=100)
            docs = text_splitter.split_documents(raw_docs)
            logging.info(f"Split the document into {len(docs)} chunks.")
            return docs
        except Exception as e:
            logging.error(f"Error loading document: {str(e)}")
            return []

    def create_vector_store(self, docs):
        """
        Create a FAISS vector store from the provided documents.
        :param docs: List of Document objects.
        :return: FAISS vector store.
        """
        try:
            embeddings = OpenAIEmbeddings(openai_api_key=OPENAI_API_KEY)
            vector_store = FAISS.from_documents(docs, embeddings)
            logging.info("Created vector store from documents.")
            return vector_store
        except Exception as e:
            logging.error(f"Error creating vector store: {str(e)}")
            return None

 
   
    def get_response(self, vector_store, question):
        """
        Get an academic response to the user's question using the vector store with guardrails.
       
        :param vector_store: FAISS vector store.
        :param question: User's question string.
        :return: Academic response string.
        """
        try:
            # Configure the retriever to fetch the top 3 relevant documents
            retriever = vector_store.as_retriever(search_kwargs={"k": 3})

            # Define a custom prompt template with academic constraints
            prompt_template = """
            You are an academic assistant. Provide a clear, concise, and well-structured academic response to the question below.
            Base your answer solely on the provided documents. Do not introduce information not contained within these documents.
            Ensure that your response adheres to academic standards, including proper terminology and a formal tone.
            Remember that you are a Teaching assistant you should not answer things not related to Documents Below, and please answer an answer that is garuanettted to be understood by the student.
            Question: {question}

            Documents:
            {context}

            Response:
            """

            # Initialize the prompt with the defined template
            prompt = PromptTemplate(
                template=prompt_template,
                input_variables=["question", "context"]
            )

            # Initialize the RetrievalQA chain with the custom prompt via chain_type_kwargs
            qa_chain = RetrievalQA.from_chain_type(
                llm=self.llm,
                chain_type="stuff",
                retriever=retriever,
                chain_type_kwargs={"prompt": prompt}
            )

            # Generate the response
            response = qa_chain.run(question)
            logging.info("Generated academic response to user question.")
            return response.strip()

        except Exception as e:
            logging.error(f"Error generating response: {str(e)}")
            return "Sorry, I couldn't process your question at the moment."

    def summarize_documents(self, documents):
            """
            Summarize the list of documents using the LLM.
            :param documents: List of Document objects.
            :return: Summary string.
            """
            try:
                # Use the summarize chain with map_reduce to handle large documents
                chain = load_summarize_chain(self.llm, chain_type="map_reduce")
                summary = chain.run(documents)
                logging.info("Generated summary of course materials.")
                return summary.strip()
            except Exception as e:
                logging.error(f"Error during summarization: {str(e)}")
                return "Sorry, I couldn't summarize the course materials at the moment."

    def generate_mcq_questions(self, documents, num_questions=10):
        """
        Generate multiple-choice questions from the documents using the LLM.
        :param documents: List of Document objects.
        :param num_questions: Number of MCQs to generate.
        :return: String containing the MCQs.
        """
        try:
            # Combine all documents into one text
            combined_text = "\n\n".join([doc.page_content for doc in documents])

            # Define the prompt template
            template = """
            You are a teacher creating assessment materials.
            Based on the following text, generate {num_questions} multiple-choice questions.

            Text:
            {text}

            Remember to ensure that the questions are clear and the options are not misleading.
            """

            prompt = PromptTemplate(
                input_variables=["num_questions", "text"],
                template=template
            )

            llm_chain = LLMChain(llm=self.llm, prompt=prompt)
            response = llm_chain.run(num_questions=num_questions, text=combined_text)
            logging.info("Generated MCQs from course materials.")
            return response.strip()
        except Exception as e:
            logging.error(f"Error generating MCQs: {str(e)}")
            return "Sorry, I couldn't generate MCQs from the course materials at the moment."

    def generate_flashcards(self, documents, num_flashcards=20):
        """
        Generate flashcards from the documents using the LLM.
        :param documents: List of Document objects.
        :param num_flashcards: Number of flashcards to generate.
        :return: String containing the flashcards.
        """
        try:
            # Combine all documents into one text
            combined_text = "\n\n".join([doc.page_content for doc in documents])

            # Define the prompt template
            template = """
            You are an expert teacher creating study materials for students.
            Based on the following text, generate {num_flashcards} flashcards.

            Each flashcard should be formatted as:
            Q: [Question]
            A: [Answer]

            The questions should cover key concepts, definitions, and important details.

            Text:
            {text}

            Remember to ensure that the questions are clear and concise, focusing on essential information.
            """

            prompt = PromptTemplate(
                input_variables=["num_flashcards", "text"],
                template=template
            )

            llm_chain = LLMChain(llm=self.llm, prompt=prompt)
            response = llm_chain.run(num_flashcards=num_flashcards, text=combined_text)
            logging.info("Generated flashcards from course materials.")
            return response.strip()
        except Exception as e:
            logging.error(f"Error generating flashcards: {str(e)}")
            return "Sorry, I couldn't generate flashcards from the course materials at the moment."

    def generate_podcast_script(self, extracted_text, openai_api_key):
        """
        Generate a podcast script using LangChain and OpenAI.
        """
        try:
            # Define the prompt template
            prompt_template = ChatPromptTemplate.from_messages([
                ("system", "You are an expert podcaster. Create a detailed and engaging podcast script based on the following content."),
                ("user", "{content}")
            ])

            # Create an LLM chain with the provided OpenAI API key
            langchain_llm = ChatOpenAI(
                model="gpt-4",
                temperature=0.7,
                openai_api_key=openai_api_key
            )
            chain = LLMChain(llm=langchain_llm, prompt=prompt_template)

            # Run the chain
            script = chain.run(content=extracted_text)
            logging.info("Generated podcast script.")
            return script.strip()
        except Exception as e:
            logging.error(f"Error generating podcast script: {e}")
            return ""

    def generate_podcast_audio(self, script, output_filename):
        """
        Convert the podcast script to speech using gTTS.
        """
        try:
            tts = gTTS(text=script, lang='en')
            temp_dir = tempfile.gettempdir()
            audio_path = os.path.join(temp_dir, output_filename)
            tts.save(audio_path)
            logging.info(f'Audio content saved to "{audio_path}"')
            return audio_path
        except Exception as e:
            logging.error(f"Error converting text to speech with gTTS: {e}")
            return ""


# Initialize LangchainHandler
langchain_handler = LangchainHandler(llm=llm)

# ---------------------- Helper Functions ----------------------
def set_bg_image():
    st.markdown(
        """
        <style>
        .stApp {
            background-color: white !important;
        }
        </style>
        """,
        unsafe_allow_html=True
    )


# ---------------------- Page Functions ----------------------

bg_image_path = "img/myim.png"
set_bg_image()

def signup_page():
    inject_css()
    bg_image_path = "img/courses-6.jpg"
    torn_edge_path = "img/overlay-top.png"

    bg_image_url = set_overlay_bg_image(bg_image_path)
    torn_edge_url = set_overlay_bg_image(torn_edge_path)

    st.markdown(f"""
        <style>
            .overlay-container {{
                position: relative;
                width: 100%;
                height: 300px;
                margin: 0 auto;
                background-image: url("{bg_image_url}");
                background-size: cover;
                background-position: center;
                display: flex;
                justify-content: center;
                align-items: center;
                overflow: hidden;
            }}
            .overlay {{
                background-color: rgba(58, 118, 240, 0.6);
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                z-index: 1;
            }}
            .overlay-content {{
                position: relative;
                z-index: 2;
                color: white;
                text-align: center;
                font-family: Arial, sans-serif;
            }}
            .overlay-content h1 {{
                font-size: 36px;
                font-weight: bold;
                margin: 0;
            }}
            .overlay-content h2 {{
                font-size: 18px;
                margin: 10px 0 0;
            }}
            .torn-edge {{
                position: absolute;
                bottom: -17px;
                left: 0;
                width: 100%;
                height: 60px;
                background-image: url("{torn_edge_url}");
                background-size: 100% 100%;
                background-repeat: no-repeat;
                transform: scaleY(-1);
                z-index: 2;
                margin-top: 0px;
            }}
        </style>
        <div class="overlay-container">
            <div class="overlay"></div>
            <div class="overlay-content">
                <h1>Sign Up</h1>
                <h2 style="color: white;">Home &raquo; Sign Up</h2>
            </div>
        </div>
        <div class="torn-edge"></div>
    """, unsafe_allow_html=True)
    st.markdown("""
        <style>
            .contact-left {
                background-color: #f4f8fd;
                padding: 20px;
                border-radius: 10px;
                width: 100%;
                margin-top: 50px;
            }
            .contact-item {
                display: flex;
                align-items: center;
                margin-bottom: 20px;
            }
            .contact-item-icon {
                width: 50px;
                height: 50px;
                border-radius: 8px;
                display: flex;
                justify-content: center;
                align-items: center;
                font-size: 24px;
                color: white;
                margin-right: 15px;
            }
            .icon-blue {
                background-color: #0a73e8;
            }
            .icon-red {
                background-color: #ff4b5c;
            }
            .icon-yellow {
                background-color: #ffc107;
            }
            .contact-item-text h4 {
                margin: 0 0 5px 0 !important;
                font-size: 18px;
                font-weight: bold;
                color: #333;
            }
            .contact-item-text p {
                margin: 0 !important;
                font-size: 14px;
                color: #666;
            }
        </style>
    """, unsafe_allow_html=True)
    st.markdown("""
        <style>
            body {
                background-color: #f9f9f9;
                font-family: Arial, sans-serif;
            }
            .container {
                max-width: 600px;
                margin: 50px auto;
                background-color: #fff;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                text-align: center;
            }
            h2 {
                font-size: 28px;
                color: #0C013E;
                margin-bottom: 20px;
                text-align: center;
            }
            p {
                font-size: 16px;
                color: #555555;
                margin-bottom: 10px;
            }
            .input-container input {
                width: 100%;
                padding: 12px;
                font-size: 14px;
                border: 1px solid #ddd;
                border-radius: 5px;
                background-color: #f4f8fd;
            }
            .input-container input:focus {
                outline: none;
                border-color: #007BFF;
                 box-shadow: 0 0 8px rgba(0, 123, 255, 0.2);
             }
             .signup-button-container {
                 display: flex; /* Use flexbox */
                 justify-content: center;
                 align-items: center;
                 margin-top: 5px;
             }
         </style>
     """, unsafe_allow_html=True)

    st.markdown("<h2 style='text-align: center;'>Sign Up</h2>", unsafe_allow_html=True)
    st.markdown("<p>Sign up now and start Contributing to Edtech!</p>", unsafe_allow_html=True)

    with st.form(key="signup_form"):
         col1, col2 = st.columns(2)
         with col1:
             username = st.text_input("Username", key="new_username")
         with col2:
             password = st.text_input("Password", type="password", key="new_password")
       
         col3, col4 = st.columns(2)
         with col3:
             confirm_password = st.text_input("Confirm Password", type="password", key="confirm_password")
         with col4:
             role = st.selectbox("Role", ["professor", "student"], key="new_role")

         # Wrap the submit button in a styled container
         st.markdown('<div class="signup-button-container">', unsafe_allow_html=True)
         submit_button = st.form_submit_button(label="Sign Up", help="Click to submit your signup form.")
         st.markdown("</div>", unsafe_allow_html=True)

         if submit_button:
             # Validation
             if not username.strip() or not password or not confirm_password or role == "Select":
                 st.markdown('<div class="error-message">Please fill out all fields.</div>', unsafe_allow_html=True)
             elif password != confirm_password:
                 st.markdown('<div class="error-message">Passwords do not match.</div>', unsafe_allow_html=True)
             elif session_db.query(User).filter_by(username=username.strip()).first():
                 st.markdown('<div class="error-message">Username already exists. Please choose another one.</div>', unsafe_allow_html=True)
             else:
                 # Add user to the database
                 new_user = User(username=username.strip(), role=role)
                 new_user.set_password(password)  # Assuming your User model has a set_password method
                 session_db.add(new_user)
                 session_db.commit()
                 st.markdown('<div class="success-message">Account created successfully! You can now log in.</div>', unsafe_allow_html=True)
                 st.info("Please switch to the Login page.")

def login_page():
    """
    Displays the login and signup page.
    """
    inject_css()
     # Paths to the background image and torn edge graphic
    bg_image_path = "img/courses-6.jpg"
    torn_edge_path = "img/overlay-top.png"

    bg_image_url = set_overlay_bg_image(bg_image_path)
    torn_edge_url = set_overlay_bg_image(torn_edge_path)

    # CSS for the overlay container and torn edge
    st.markdown(f"""
        <style>
            .overlay-container {{
                position: relative;
                width: 100%;
                height: 300px;
                margin: 0 auto;
                background-image: url("{bg_image_url}");
                background-size: cover;
                background-position: center;
                display: flex;
                justify-content: center;
                align-items: center;
                overflow: hidden;
            }}
            .overlay {{
                background-color: rgba(58, 118, 240, 0.6);
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                z-index: 1;
            }}
            .overlay-content {{
                position: relative;
                z-index: 2;
                color: white;
                text-align: center;
                font-family: Arial, sans-serif;
            }}
            .overlay-content h1 {{
                font-size: 36px;
                font-weight: bold;
                margin: 0;
            }}
            .overlay-content h2 {{
                font-size: 18px;
                margin: 10px 0 0;
            }}
            .torn-edge {{
                position: absolute;
                bottom: -17px;
                left: 0;
                width: 100%;
                height: 60px;
                background-image: url("{torn_edge_url}");
                background-size: 100% 100%;
                background-repeat: no-repeat;
                transform: scaleY(-1);
                z-index: 2;
                margin-top: 0px;
            }}
        </style>
        <div class="overlay-container">
            <div class="overlay"></div>
            <div class="overlay-content">
                <h1>Login</h1>
                <h2 style="color: white;">Home &raquo; Login</h2>
            </div>
        </div>
        <div class="torn-edge"></div>
    """, unsafe_allow_html=True)
    st.markdown("""
        <style>
            .contact-left {
                background-color: #f4f8fd;
                padding: 20px;
                border-radius: 10px;
                width: 100%;
                margin-top: 50px;
            }
            .contact-item {
                display: flex;
                align-items: center;
                margin-bottom: 20px;
            }
            .contact-item-icon {
                width: 50px;
                height: 50px;
                border-radius: 8px;
                display: flex;
                justify-content: center;
                align-items: center;
                font-size: 24px;
                color: white;
                margin-right: 15px;
            }
            .icon-blue {
                background-color: #0a73e8;
            }
            .icon-red {
                background-color: #ff4b5c;
            }
            .icon-yellow {
                background-color: #ffc107;
            }
            .contact-item-text h4 {
                margin: 0 0 5px 0 !important;
                font-size: 18px;
                font-weight: bold;
                color: #333;
            }
            .contact-item-text p {
                margin: 0 !important;
                font-size: 14px;
                color: #666;
            }
        </style>
    """, unsafe_allow_html=True)
    st.markdown("""
        <style>
            body {
                background-color: #f9f9f9;
                font-family: Arial, sans-serif;
            }
            .container {
                max-width: 600px;
                margin: 50px auto;
                background-color: #fff;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                text-align: center;
            }
            h2 {
                font-size: 28px;
                color: #CCCCCC;
                margin-bottom: 20px;
                text-align: center;
            }
            p {
                font-size: 16px;
                color: #555555;
                margin-bottom: 30px;
            }
            .input-container input {
                width: 100%;
                padding: 12px;
                font-size: 14px;
                border: 1px solid #ddd;
                border-radius: 5px;
                background-color: #f4f8fd;
            }
            .input-container input:focus {
                outline: none;
                border-color: #007BFF;
                box-shadow: 0 0 8px rgba(0, 123, 255, 0.2);
            }
            .signup-button-container {
                display: flex;
                justify-content: center;
                align-items: center;
                margin-top: 30px;
            }
        </style>
    """, unsafe_allow_html=True)
    st.markdown("<h2 style='text-align: center;'>Login</h2>", unsafe_allow_html=True)
    with st.form(key='login_form'):
            col1, col2 = st.columns(2)
            with col1:
                username = st.text_input("Username", key="new_username")
            with col2:
                password = st.text_input("Password", type="password", key="new_password")
           
            submit = st.form_submit_button("Login")
            if submit:
                if not username.strip() or not password:
                    st.error("Please enter both username and password.")
                    return
                user = session_db.query(User).filter_by(username=username.strip()).first()
                if user and user.check_password(password):
                    st.session_state.user = user
                    st.session_state.page = "dashboard"
                    st.success("Logged in successfully!")
                else:
                    st.error("Invalid username or password.")

def professor_page():
    if 'selected_tab' not in st.session_state:
        st.session_state.selected_tab = "Create Course"

    # Override Styles to Ensure a Full Dark Theme  
   
    st.markdown("""
        <style>
        /* Global Dark Background and Text */
        html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stVerticalBlock"] {
            background-color: #121212 !important;
            color: #FFFFFF !important;
        }

        /* Override Streamlit's main container background */
        [data-testid="stAppViewContainer"] > div {
            background-color: #121212 !important;
        }

        /* Sidebar Styling */
        .sidebar {
            position: fixed;
            top: 0;
            left: 0;
            height: 100%;
            width: 250px;
            background-color: #1E1E1E;
            color: #FFFFFF;
            padding: 20px;
            box-shadow: 2px 0 5px rgba(0, 0, 0, 0.5);
        }
        .sidebar-header {
            font-size: 24px;
            font-weight: 600;
            text-align: center;
            margin-bottom: 40px;
            margin-top: 40px;
            color: #BB86FC;
            border-bottom: 1px solid #333;
            padding-bottom: 10px;
        }

        /* Buttons */
        .stButton button {
            width: 100%;
            background-color: #2C2C2C !important;
            color: #FFFFFF !important;
            border: 1px solid #3C3C3C !important;
            border-radius: 8px !important;
            padding: 10px !important;
            margin-top: 10px !important;
            font-weight: 500 !important;
            font-size: 16px !important;
        }
        .stButton button:hover {
            background-color: #3C3C3C !important;
            border-color: #BB86FC !important;
            color: #BB86FC !important;
        }

        /* Logged-in Section */
        .logged-in {
            font-size: 14px;
            margin-top: 20px;
            color: #AAAAAA;
        }

        /* Logout Button */
        .stButton button.logout-button {
            background-color: #BB86FC !important;
            color: #121212 !important;
            border: none !important;
            margin-top: 20px !important;
        }
        .stButton button.logout-button:hover {
            background-color: #9E63E3 !important;
        }

        /* Main Title */
        h1 {
            color: #BB86FC;
            font-weight: 600;
            font-size: 32px;
            margin-top: 20px;
        }

        /* Divider */
        hr.custom-divider {
            border: none;
            border-top: 2px solid #333;
            margin: 20px 0;
        }

        /* Main content spacing */
        .main-content {
            margin-left: 270px;
            padding: 20px;
        }
        </style>
    """, unsafe_allow_html=True)

    # Main Title and Divider
    st.markdown("<div class='main-content'>", unsafe_allow_html=True)
    st.markdown("<h1>Professor Dashboard</h1>", unsafe_allow_html=True)
    st.markdown("<hr class='custom-divider' />", unsafe_allow_html=True)

    # Sidebar Design
    with st.sidebar:
        st.markdown("<div class='sidebar'>", unsafe_allow_html=True)
        st.markdown("<div class='sidebar-header'>Navigation</div>", unsafe_allow_html=True)

        # Dynamic Buttons for Navigation
        if st.button("Create Course", key="create_course"):
            st.session_state.selected_tab = "Create Course"

        if st.button("Manage Courses", key="manage_courses"):
            st.session_state.selected_tab = "Manage Courses"

        # Logged-In User Info
        st.markdown("<div class='logged-in'>Logged in as:</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='logged-in'>{st.session_state.user.username}</div>", unsafe_allow_html=True)

        # Logout Button
        # if st.button("Logout", key="logout", help="Logout from your account", args=None, kwargs=None):
        #     st.session_state.user = None
        #     st.session_state.page = "home"

        st.markdown("</div>", unsafe_allow_html=True)

    # Main Content
    if st.session_state.selected_tab == "Create Course":
        create_course_section()
    elif st.session_state.selected_tab == "Manage Courses":
        manage_courses_section()

    st.markdown("</div>", unsafe_allow_html=True)


def home_page():
    inject_css()

    bg_image_path = "img/thinksmarter.png"
    torn_edge_path = "img/overlay-top.png"

    bg_image_url = set_overlay_bg_image(bg_image_path)
    torn_edge_url = set_overlay_bg_image(torn_edge_path)

    st.markdown(f"""
        <style>
            /* Increase the maximum width of the main content area */
            .main .block-container {{
                max-width: 1400px;
                margin: 0 auto;
            }}
            .overlay-container {{
                position: relative;
                width: 100%;
                height: 300px;
                background-image: url("{bg_image_url}");
                background-size: cover;
                background-position: center;
                display: flex;
                justify-content: center;
                align-items: center;
                overflow: hidden;
            }}
            .overlay {{
                background-color: rgba(58, 118, 240, 0.6);
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                z-index: 1;
            }}
            .overlay-content {{
                position: relative;
                z-index: 2;
                color: white;
                text-align: center;
                font-family: Arial, sans-serif;
            }}
            .overlay-content h1 {{
                font-size: 36px;
                font-weight: bold;
            }}
            .overlay-content h2 {{
                font-size: 18px;
                margin-top: 10px;
            }}
            .torn-edge {{
                position: absolute;
                bottom: -17px;
                left: 0;
                width: 100%;
                height: 60px;
                background-image: url("{torn_edge_url}");
                background-size: cover;
                background-repeat: no-repeat;
                transform: scaleY(-1);
                z-index: 2;
            }}
            .section-wrapper {{
                margin-top: 50px;
            }}
            .about-title {{
                text-align: left;
                width: 100%;
                font-size: 24px;
                font-weight: normal;
                margin: 0 0 10px 0;
                padding: 0;
            }}
       
            .about-section {{
                display: flex;
                flex-direction: row-reverse;
                align-items: flex-start;
                justify-content: space-between;
                color: white;
                padding: 20px;
                border-radius: 10px;
                width: 100%;
                box-sizing: border-box;
            }}
            .about-content {{
                flex: 1;
                margin: 10px;
                line-height: 1.6;
                font-size: 16px;
            }}
            .about-content h2 {{
                color: #1C1C44;
                margin-bottom: 20px;
                text-align: left;
                margin-left: -10px;
            }}
            .about-content p {{
                font-size: 16px;
                line-height: 1.8;
                color: #444;
            }}
            .about-image {{
                position: relative;
                top: 180px;
                flex: 1;
                margin-right: 20px;
                margin-left: 20px;
                display: flex;
                justify-content: flex-start;
                align-items: flex-start;
            }}
            .about-image img{{
                width: 100%;
                height: auto;
            }}
            .stats-section {{
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
                gap: 0px;
                width: 100%;
                box-sizing: border-box;
            }}
            .stat-card {{
                flex: 0 1 25px;
                text-align: center;
                padding: 20px;
                border-radius: 0;
                color: white;
                font-family: Arial, sans-serif;
            }}
            .stat-card .stat-number {{
                font-size: 36px;
                font-weight: bold;
            }}

            .stat-card .stat-label {{
                font-size: 16px;
                margin-top: 10px;
            }}
            .stat-card.green {{
                background-color: #28a745;
            }}
            .stat-card.blue {{
                background-color: #007bff;
            }}
            .stat-card.red {{
                background-color: #dc3545;
            }}
            .stat-card.yellow {{
                background-color: #ffc107;
            }}
            .stat-box {{
                text-align: center;
                padding: 20px;
                border-radius: 10px;
                color: white;
                width: 150px;
                font-family: Arial, sans-serif;
            }}
            .green {{ background-color: #28A745; }}
            .blue {{background-color: #007BFF; }}
            .red {{background-color: #DC3545; }}
            .yellow {{background-color: #FFC107; }}
            .feature-block {{
                display: flex;
                margin: 20px 0;
                align-items: center;
            }}
            .feature-block img {{
                background-color: #004CFF;
                padding: 20px;
                border-radius: 50%;
                width: 70px;
                height: 70px;
                margin-right: 20px;
            }}
            .features-section {{
                display: flex;
                flex-direction: column;
                gap: 20px;
                padding: 10px;
                margin-bottom: 40px;
                width: 100%;
                box-sizing: border-box;
            }}
            .feature-item {{
                display: flex;
                align-items: center;
                gap: 20px;
                padding: 15px;
                border-radius: 8px;
                background-color: #ffffff;
                box-shadow: 0px 2px 4px rgba(0, 0, 0, 0.1);
                max-width: 600px;
                overflow: hidden;
            }}
            .feature-icon {{
                display: flex;
                justify-content: center;
                align-items: center;
                width: 50px;
                height: 70px;
                border-radius: 10px;
            }}
            .feature-icon-blue {{
                background-color: #007bff;
            }}
            .feature-icon-red {{
                background-color: #dc3545;
            }}
            .feature-icon-yellow {{
                background-color: #ffc107;
            }}
            .feature-icon img {{
                width: 40px;
                height: 40px;
            }}
            .feature-text h3 {{
                margin: 0 0 5px 0;
                font-size: 18px;
                font-weight: bold;
                color: #1c1c44;
            }}
            .feature-text p {{
                margin: 0;
                font-size: 14px;
                line-height: 1.6;
                color: #555;
            }}
            .graduate-image {{
                position: relative;
                top: 50px;
                flex: 1;
                max-width: 400px;
                max-height: 400px;
                margin-right: 20px;
                margin-left: 20px;
                display: flex;
                justify-content: flex-start;
                align-items: flex-start;
            }}
            .graduate-image img{{
                width: 100%;
                height: auto;
            }}

            /* Aligning the video better */
            .about-video {{
                flex: 1;
                display: flex;
                justify-content: center;
                align-items: center;
                margin: 10px;
                position: relative;
            }}
            .about-video video {{
                max-width: 100%;
                height: auto;
                border-radius: 10px;
            }}
        </style>
    """, unsafe_allow_html=True)
    st.markdown(f"""
        <style>
            .overlay-container {{
                position: relative;
                width: 100%;
                height: 300px;
                background-image: url("{bg_image_url}");
                background-size: cover;
                background-position: center;
                display: flex;
                justify-content: center;
                align-items: center;
            }}
        </style>
        <div class="overlay-container"></div>
    """, unsafe_allow_html=True)

    video_path = "img/upload_your_sources.mp4"  # Replace with your actual video path
    base64_video = encode_video_to_base64(video_path)  # Ensure this function is defined
    st.markdown(f"""
        <style>
            .about-section {{
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                width: 100%;
                color: #1C1C44;
                padding: 20px;
                box-sizing: border-box;
                text-align: center;
            }}
            .about-content {{
                max-width: 800px;
                margin: 0 auto 30px auto;
                font-size: 16px;
                line-height: 1.8;
                color: #444;
            }}
            .about-content h2 {{
                text-align: left;
                font-size: 30px;
                font-weight: bold;
                margin-bottom: 20px;
            }}
            .about-video {{
                width: 100%;
                display: flex;
                justify-content: center;
                align-items: center;
                position: relative;
                margin-top: 20px;
            }}
            .about-video video {{
                max-width: 1000px;
                width: 100%;      
                height: auto;
                border-radius: 10px;
                margin: 0 auto;
            }}
        </style>

        <div class="about-section">
            <div class="about-content">
                <h2>Your Personalized AI Teaching Assistant With all of your sources in place</h2>
                <p>Experience a revolutionary way to learn with our AI-powered educational platform. Combining the latest in artificial intelligence technology, our platform offers personalized learning experiences through an interactive AI chatbot, engaging flashcards, and adaptive learning tools. Whether you're preparing for exams, mastering new skills, or expanding your knowledge, our platform tailors content to your unique needs, helping you learn faster and more effectively. Discover a smarter, more efficient way to achieve your educational goals with the power of AI at your fingertips.</p>
            </div>
            <div class="about-video">
                <video autoplay loop muted controls>
                    <source src="data:video/mp4;base64,{base64_video}" type="video/mp4">
                    Your browser does not support the video tag.
                </video>
            </div>
        </div>
    """, unsafe_allow_html=True)


    st.markdown("""
        <div class="stats-section">
            <div class="stat-card green">
                <div class="stat-label">UPLOAD RESOURCES</div>
            </div>
            <div class="stat-card blue">
                <div class="stat-label">INSTANT INSIGHTS</div>
            </div>
            <div class="stat-card red">
                <div class="stat-label">ASSESS KNOWLEDGE</div>
            </div>
            <div class="stat-card yellow">
                <div class="stat-label">LEARN WITH PODCASTER</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    st.markdown(f"""
        <div class="about-section">
            <div class="graduate-image">
            </div>
            <div class="about-content">
                <h2 style="text-align: left; font-size: 30px; font-weight: bold; margin-bottom: 20px;">Why Should You Start Learning with Us?</h2>
                <p>Join a platform that truly understands your learning needs. Our commitment to innovation ensures a unique, engaging, and personalized education experience. With tools designed to simplify complex topics and foster deep understanding, we empower learners to achieve their goals faster and more effectively. Experience the perfect blend of technology and expertise to take your learning journey to the next level. Let’s make your success our mission!</p>
            </div>
        </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="features-section">
        <div class="feature-item">
            <div class="feature-icon feature-icon-blue">
                <img src="https://img.icons8.com/ios-filled/50/ffffff/graduation-cap.png" alt="Skilled Instructors Icon">
            </div>
            <div class="feature-text">
                <h3>Power study</h3>
                <p>Upload lecture recordings, textbook chapters, and research papers..</p>
            </div>
        </div>
        <div class="feature-item">
            <div class="feature-icon feature-icon-red">
                <img src="https://img.icons8.com/ios-filled/50/ffffff/certificate.png" alt="International Certificate Icon">
            </div>
            <div class="feature-text">
                <h3>Organize your thinking</h3>
                <p>create a polished presentation outline, complete with key talking points and supporting evidence..</p>
            </div>
        </div>
        <div class="feature-item">
            <div class="feature-icon feature-icon-yellow">
                <img src="https://img.icons8.com/ios-filled/50/ffffff/class.png" alt="Online Classes Icon">
            </div>
            <div class="feature-text">
                <h3>Spark new ideas</h3>
                <p>Fit learning into your schedule using podcasts, anytime, anywhere.</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    inject_css2()

# --- Helper Functions ---

def classify_topic(question):
    """
    Classifies a question into an ML topic based on keyword matching.
   
    :param question: The question text.
    :return: The determined topic as a string.
    """
    keyword_topic_map = {
        'regression': 'Regression',
        'classification': 'Classification',
        'clustering': 'Clustering',
        'neural network': 'Neural Networks',
        'deep learning': 'Deep Learning',
        'supervised learning': 'Supervised Learning',
        'unsupervised learning': 'Unsupervised Learning',
        'reinforcement learning': 'Reinforcement Learning',
        'dimensionality reduction': 'Dimensionality Reduction',
        'decision tree': 'Decision Trees',
        'random forest': 'Random Forest',
        'support vector machine': 'Support Vector Machines',
        'k-means': 'K-Means Clustering',
        'principal component analysis': 'PCA',
        'natural language processing': 'NLP',
        'computer vision': 'Computer Vision',
        'gradient descent': 'Optimization',
        'overfitting': 'Model Evaluation',
        'underfitting': 'Model Evaluation',
        'cross-validation': 'Model Evaluation',
        # Add more mappings as needed
    }
   
    question_lower = question.lower()
   
    for keyword, topic in keyword_topic_map.items():
        if re.search(r'\b' + re.escape(keyword) + r'\b', question_lower):
            return topic
   
    return 'General'  # Default topic if no keywords match

def update_course_csv(csv_file_path, question, topic):
    """
    Appends a new question with its topic to the specified CSV file.
   
    :param csv_file_path: Path to the CSV file.
    :param question: The question text.
    :param topic: The classified topic of the question.
    """
    try:
        # Log the CSV file path for debugging
        logging.debug(f"CSV File Path: '{csv_file_path}'")
       
        # Extract directory from the CSV file path
        directory = os.path.dirname(csv_file_path)
       
        # Only attempt to create directories if a directory path is provided
        if directory:
            os.makedirs(directory, exist_ok=True)
            logging.debug(f"Ensured directory exists: '{directory}'")
        else:
            logging.debug("No directory specified. Using current working directory.")
       
        # Check if the CSV file exists
        file_exists = os.path.isfile(csv_file_path)
       
        # Open the CSV file in append mode
        with open(csv_file_path, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Topic', 'Question']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
           
            # If the file doesn't exist, write the header
            if not file_exists:
                writer.writeheader()
                logging.debug("CSV header written.")
           
            # Write the new row
            writer.writerow({
                'Topic': topic,
                'Question': question
            })
       
        logging.info(f"Appended question to CSV: Topic='{topic}', Question='{question}'")
   
    except Exception as e:
        logging.error(f"Failed to update CSV: {e}")

# --- Main Function ---

def generate_youtube_keyword(api_key, query):
    """
    Generate a YouTube search keyword using LangChain.
    """
    chat = ChatOpenAI(openai_api_key=api_key, model="gpt-3.5-turbo", temperature=0.7)
    messages = [
        SystemMessage(content="You are an expert at generating YouTube search keywords."),
        HumanMessage(content=f"Suggest a good YouTube search keyword for this topic: {query}")
    ]
    try:
        response = chat.invoke(messages)
        return response.content.strip()
    except Exception as e:
        return f"Error: {e}"

def search_youtube(keyword, num_results=10):
    """
    Search YouTube and return the specified number of video links.
    """
    search_keyword = keyword.replace(" ", "+")
    url = f"https://www.youtube.com/results?search_query={search_keyword}"
    html = urllib.request.urlopen(url).read()
    soup = BeautifulSoup(html, 'html.parser')
    video_ids = re.findall(r"watch\?v=(\S{11})", str(soup))
    return [f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids[:num_results]] if video_ids else []

def download_transcripts(video_links, folder_path="transcripts"):
    """
    Download video transcripts and save them to a folder.
    """
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

    transcripts = {}
    for i, video_link in enumerate(video_links):
        video_id = video_link.split("=")[-1]
        transcript_path = os.path.join(folder_path, f"transcript_{video_id}.txt")
        # Simulated transcript fetching  
        #here just for purpose of demo i am not fetching the transcript via API  just for delays, for the full process we will call 
        #the function to get the transcript from the video used previously to get the transcript in the embedding video up.  
        fake_transcript = f"Transcript for video {video_id}" 
        with open(transcript_path, 'w', encoding='utf-8') as file:
            file.write(fake_transcript)
        transcripts[video_id] = fake_transcript
    return transcripts

def embed_transcripts(transcripts, model_name='all-MiniLM-L6-v2'):
    """
    Generate embeddings for transcripts.
    """
    model = SentenceTransformer(model_name)
    embeddings = {}
    for video_id, transcript in transcripts.items():
        embedding = model.encode(transcript, convert_to_numpy=True)
        embeddings[video_id] = embedding
    return embeddings

def recommend_video(query_embedding, video_embeddings):
    """
    Recommend the most relevant video based on similarity to the query embedding.
    """
    video_ids = list(video_embeddings.keys())
    embeddings = np.array(list(video_embeddings.values()))
    similarities = cosine_similarity([query_embedding], embeddings)[0]
    top_index = similarities.argmax()
    return video_ids[top_index], similarities[top_index]

def student_page():
    # Apply a dark theme similar to the professor page
    st.markdown("""
    <style>
    /* Global Dark Background and Text */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stVerticalBlock"] {
        background-color: #121212 !important;
        color: #84D8B6 !important;
    }

    [data-testid="stAppViewContainer"] > div {
        background-color: #121212 !important;
    }

   /* Styling headings and dividers */
    h1, h2, h3 {
        font-family: "Arial", sans-serif;
    }

    h1 {
        color: #FFFFFF;
        font-size: 32px;
        font-weight: 600;
        margin-top: 20px;
    }

    h2 {
        font-size: 24px;
        font-weight: 600;
        margin-bottom: 15px;
    }

    h3 {
        font-size: 20px;
        font-weight: 500;
        margin-bottom: 10px;
        color: #84D8B6;
    }


    /* Cards and Containers */
    .course-card, .hidden-details, .flashcard, .mcq, .summary, .chat-response {
        background-color: #1E1E1E;
        border-radius: 10px;
        padding: 15px;
        margin-bottom: 20px;
        box-shadow: 0 4px 10px rgba(0, 0, 0, 0.5);
        transition: background-color 0.3s ease, transform 0.2s ease-in-out;
    }
    .course-card:hover, .flashcard:hover, .mcq:hover, .summary:hover, .chat-response:hover {
        background-color: #292929;
        transform: scale(1.02);
    }

    /* Text and Links */
    p {
        color: #CCCCCC;
        font-size: 14px;
        line-height: 1.6;
    }
    a {
        color: #BB86FC;
        text-decoration: none;
    }
    a:hover {
        text-decoration: underline;
    }

    /* Buttons */
    .stButton button {
        background-color: #2C2C2C !important;
        color: #FFFFFF !important;
        border: 1px solid #ADF0D3 !important;
        border-radius: 8px !important;
        padding: 10px !important;
        margin-top: 10px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        transition: background-color 0.3s ease, border-color 0.3s ease !important;
    }
    .stButton button:hover {
        background-color: #3C3C3C !important;
        border-color: #ADF0D3 !important;
    }
    
                
    .view-details-button {
        background-color: #2C2C2C !important;
        color: #FFFFFF !important;
        border: 1px solid #ADF0D3 !important;
        border-radius: 8px !important;
        padding: 10px !important;
        margin-top: 10px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        transition: background-color 0.3s ease, border-color 0.3s ease !important;
    }
    .view-details-button:hover {
        background-color: #3C3C3C !important;
        border-color: #ADF0D3 !important;
    }
    
    .custom-error-box {
        background-color: #ffe6e6;
        border-left: 6px solid #ff4d4d;
        color: #a94442;
        padding: 10px;
        border-radius: 5px;
        font-family: Arial, sans-serif;
    }
    


    /* Response Containers */
    .flashcard, .mcq, .summary, .chat-response {
        border-left: 5px solid #4A90E2;
    }
    .flashcard h4, .mcq h4, .summary h4, .chat-response p {
        margin-bottom: 10px;
        color: #4A90E2;
    }

    /* Scrollbar */
    ::-webkit-scrollbar {
        width: 8px;
    }
    ::-webkit-scrollbar-track {
        background: #1E1E1E;
    }
    ::-webkit-scrollbar-thumb {
        background: #4A90E2;
        border-radius: 10px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #357ABD;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <style>
    .dashboard-title {
        color: white !important; /* Ensure the color is applied */
    }
    </style>
    <h1 class='dashboard-title'>My Courses</h1>
    """, unsafe_allow_html=True)

    courses = session_db.query(Course).all()

    if not courses:
        st.info("No courses available at the moment.")
        return

    if "opened_course_id" not in st.session_state:
        st.session_state.opened_course_id = None

    for course in courses:
        
        with st.container():
            # Display Course Card
            st.markdown(f"""
            <div class='course-card'>
                <h3 style="color: #ADF0D3;">{course.name}</h3>
                <p><strong>Professor:</strong> {course.professor_id}</p>
            </div>
            """, unsafe_allow_html=True)

            # View Details Button
            if st.button(f"View Details for {course.name}", key=f"view_details_{course.id}"):
                if st.session_state.opened_course_id == course.id:
                    st.session_state.opened_course_id = None
                else:
                    st.session_state.opened_course_id = course.id

            # Show Details if opened
            if st.session_state.opened_course_id == course.id:
                with st.container():
                    st.markdown("<div class='hidden-details'>", unsafe_allow_html=True)
                    st.markdown(f"<h3 style='color: #FFFFFF;'>Details for {course.name}</h3>", unsafe_allow_html=True)

                    # Podcast Feature
                    generate_podcast_for_course(course, OPENAI_API_KEY)

                    # Flashcards
                    # Flashcards Section
                    # Flashcards Section
                    st.markdown("<h3>📚 Study with Flashcards</h3>", unsafe_allow_html=True)

                    

                    # Flashcards functionality
                    with st.form(key=f'flashcards_form_{course.id}', clear_on_submit=True):
                        submit = st.form_submit_button("Generate Flashcards")  # Single button with styling and functionality
                        if submit:
                            with st.spinner("Generating flashcards..."):
                                try:
                                    flashcards = generate_flashcards_for_course(course)
                                    st.success("🃏 Here are your flashcards:")
                                    st.markdown("<div class='flashcard'>{}</div>".format(flashcards.replace('\n', '<br>')), unsafe_allow_html=True)
                                except Exception as e:
                                    st.markdown(
                                    f"""
                                    <div class="custom-error-box">
                                        <strong>An error occurred:</strong> {e}
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )


                    # MCQs
                    st.markdown("<h3>📝 Assess Your Knowledge</h3>", unsafe_allow_html=True)
                    with st.form(key=f'mcq_form_{course.id}', clear_on_submit=True):
                        submit = st.form_submit_button("Generate MCQs")
                        if submit:
                            with st.spinner("Generating MCQs..."):
                                try:
                                    mcqs = generate_mcq_for_course(course)
                                    st.success("🔍 Multiple-Choice Questions:")
                                    mcq_list = mcqs.strip().split('\n\n')
                                    for mcq_item in mcq_list:
                                        if mcq_item.strip():
                                            st.markdown("<div class='mcq'><strong>" + mcq_item.replace('\n', '<br>') + "</strong></div>", unsafe_allow_html=True)
                                except Exception as e:
                                    st.markdown(
                                    f"""
                                    <div class="custom-error-box">
                                        <strong>An error occurred:</strong> {e}
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )


                    # Summarize Course
                    st.markdown("<h3>📄 Summarize Course</h3>", unsafe_allow_html=True)
                    with st.form(key=f'summarize_form_{course.id}', clear_on_submit=True):
                        
                        submit = st.form_submit_button("Get Summary")
                        if submit:
                            with st.spinner("Generating summary..."):
                                try:
                                    summary = summarize_course_documents(course)
                                    st.success("📖 Course Summary:")
                                    st.markdown("<div class='summary'>" + summary.replace('\n', '<br>') + "</div>", unsafe_allow_html=True)
                                except Exception as e:
                                    st.markdown(
                                    f"""
                                    <div class="custom-error-box">
                                        <strong>An error occurred:</strong> {e}
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )

                    # Chat with Documents
                    st.markdown("<h3>💬 Chat with Course Material</h3>", unsafe_allow_html=True)
                    with st.form(key=f'chat_form_{course.id}', clear_on_submit=True):
                        user_question = st.text_input(f"Ask a question about {course.name}:", key=f"question_input_{course.id}")
                        submit = st.form_submit_button("Send")
                        if submit:
                            if user_question.strip():
                                with st.spinner("Processing your question..."):
                                    try:
                                        # Generate response
                                        response = chat_with_documents(course, user_question)
                                        st.success("Response:")
                                        st.markdown(f"<div class='chat-response'><p>{response}</p></div>", unsafe_allow_html=True)

                                        # Classify topic
                                        topic = classify_topic(user_question)

                                        # Define the path to your CSV file
                                        csv_file_path = os.path.join("data", "ml_grouped_topics_questions.csv")  # Specify directory

                                        # Update the CSV in real-time
                                        update_course_csv(csv_file_path, user_question, topic)

                                        st.info(f"Your question has been classified under the topic: **{topic}** and recorded.")
                                    except Exception as e:
                                        st.markdown(
                                        f"""
                                        <div class="custom-error-box">
                                            <strong>An error occurred:</strong> {e}
                                        </div>
                                        """,
                                        unsafe_allow_html=True
                                    )
                            else:
                
                                st.markdown("<p class='info-message'>Please enter a question.</p>", unsafe_allow_html=True)

                    # Add the YouTube Recommendation Feature
                    st.markdown("<h3>📺 Find Relevant YouTube Video</h3>", unsafe_allow_html=True)
                    with st.form(key=f'youtube_form_{course.id}', clear_on_submit=True):
                        youtube_query = st.text_input("Enter your query for YouTube search:", key=f"youtube_input_{course.id}")
                        submit_youtube = st.form_submit_button("Find Best Video")
                        if submit_youtube:
                            if youtube_query.strip():
                                with st.spinner("Processing your query..."):
                                    try:
                                        # Step 1: Generate a refined YouTube search keyword
                                        refined_query = generate_youtube_keyword(OPENAI_API_KEY, youtube_query)
                                        st.markdown(f"**Generated Keyword:** {refined_query}")

                                        # Step 2: Search YouTube for videos
                                        video_links = search_youtube(refined_query, num_results=3)
                                        if not video_links:
                                            st.error("No videos found. Please refine your query.")
                                        else:
                                          
                                            # Step 3: Download transcripts for the top 3 videos
                                            transcripts = download_transcripts(video_links)

                                            # Step 4: Embed transcripts
                                            video_embeddings = embed_transcripts(transcripts)

                                            # Step 5: Generate query embedding and recommend the best video
                                            model = SentenceTransformer('all-MiniLM-L6-v2')
                                            query_embedding = model.encode(youtube_query, convert_to_numpy=True)
                                            best_video_id, similarity = recommend_video(query_embedding, video_embeddings)
                                            best_video_link = f"https://www.youtube.com/watch?v={best_video_id}"
                                            st.success(f"**Best Video:** [{best_video_link}])\n")
                                            st.video(best_video_link)

                                    except Exception as e:
                                        st.markdown(
                                        f"""
                                        <div class="custom-error-box">
                                            <strong>An error occurred:</strong> {e}
                                        </div>
                                        """,
                                        unsafe_allow_html=True
                                    )
                            else:
                                st.markdown(
                                f"""
                                <div class="custom-error-box">
                                    <strong>Please enter a query</strong>
                                </div>
                                """,
                                unsafe_allow_html=True
                            )

                    st.markdown("</div>", unsafe_allow_html=True)

def generate_podcast_for_course(course, openai_api_key):
    """
    Allows students to generate a podcast based on the course materials or by uploading additional PDFs.
    """
    podcast_audio_key = f"podcast_audio_{course.id}"
    script_key = f"script_{course.id}"
    if podcast_audio_key not in st.session_state:
        st.session_state[podcast_audio_key] = ""
    if script_key not in st.session_state:
        st.session_state[script_key] = ""

    st.markdown("<h3 style='color: #84D8B6;'>🎙 Generate Podcast for This Course</h3>", unsafe_allow_html=True)

    with st.container():
        st.markdown("<h4 style='color: White;'>Upload Additional PDF File(s) for Podcast</h4>", unsafe_allow_html=True)
        uploaded_files = st.file_uploader(
            f" Upload PDF File(s) for {course.name}",
            accept_multiple_files=True,
            type=['pdf'],
            key=f"podcast_upload_{course.id}"
        )

        st.markdown("""
<style>
div[role="alert"],
div[role="alert"] * {
    color: black !important;
}
</style>
""", unsafe_allow_html=True)
        generate_btn = st.button(" Generate Podcast", key=f"generate_podcast_{course.id}")

        if generate_btn:
            if not uploaded_files:
                st.error("Please upload at least one PDF file to generate a podcast.")
                return

            all_text = ""
            for uploaded_file in uploaded_files:
                st.info(f"Processing file: {uploaded_file.name}")
                extracted_text = extract_text_from_pdf(uploaded_file)
                if extracted_text:
                    all_text += extracted_text + "\n"
                    st.success(f"Extracted text from {uploaded_file.name}")
                else:
                    st.warning(f"No text extracted from {uploaded_file.name}.")

            if all_text:
                st.info("Generating podcast script...")
                script = langchain_handler.generate_podcast_script(all_text, openai_api_key)
                if script:
                    st.session_state[script_key] = script
                    st.success("Podcast script generated successfully!")

                    if st.checkbox(" View Generated Script", key=f"view_script_{course.id}"):
                        st.markdown("### Generated Podcast Script")
                        st.write(script)

                    st.info("Converting script to audio...")
                    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                    output_filename = f"podcast_{course.id}_{timestamp}.mp3"
                    podcast_audio_path = langchain_handler.generate_podcast_audio(script, output_filename)

                    if podcast_audio_path:
                        st.session_state[podcast_audio_key] = podcast_audio_path
                        st.success("Audio podcast generated successfully!")
                        st.markdown("###  Listen to Your Podcast")
                        st.audio(podcast_audio_path, format='audio/mp3')
                    else:
                        st.error("Failed to convert script to audio.")
                else:
                    st.error("Failed to generate podcast script.")
            else:
                st.error("No text extracted from the uploaded files.")

def chat_with_documents(course, question):
    documents = []
    for file in course.files:
        try:
            # Save the file to a temporary location
            with tempfile.NamedTemporaryFile(delete=False, suffix=file.filename) as tmp_file:
                tmp_file.write(file.data)
                tmp_file_path = tmp_file.name
            docs = langchain_handler.load_document(tmp_file_path)
            if docs:
                documents.extend(docs)
        except Exception as e:
            st.warning(f"Could not process file {file.filename}: {type(e).__name__}: {e}")
            continue

    if not documents:
        raise ValueError("No readable course materials available.")

    vector_store = langchain_handler.create_vector_store(documents)
    if not vector_store:
        raise ValueError("Failed to create vector store from documents.")

    response = langchain_handler.get_response(vector_store, question)

    # Store the student's question
    if st.session_state.user and st.session_state.user.role == 'student':
        try:
            new_question = StudentQuestion(
                user_id=st.session_state.user.id,
                course_id=course.id,
                question=question
            )
            session_db.add(new_question)
            session_db.commit()
            logging.info(f"Stored question from user {st.session_state.user.username} for course {course.name}.")
        except Exception as e:
            session_db.rollback()
            logging.error(f"Error storing student question: {str(e)}")
            st.error("An error occurred while saving your question.")

    return response

def summarize_course_documents(course):
    documents = []
    for file in course.files:
        try:
            # Save the file to a temporary location
            with tempfile.NamedTemporaryFile(delete=False, suffix=file.filename) as tmp_file:
                tmp_file.write(file.data)
                tmp_file_path = tmp_file.name
            docs = langchain_handler.load_document(tmp_file_path)
            if docs:
                documents.extend(docs)
        except Exception as e:
            st.warning(f"Could not process file {file.filename}: {type(e).__name__}: {e}")
            continue

    if not documents:
        raise ValueError("No readable course materials available.")

    summary = langchain_handler.summarize_documents(documents)
    return summary

def generate_mcq_for_course(course):
    documents = []
    for file in course.files:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=file.filename) as tmp_file:
                tmp_file.write(file.data)
                tmp_file_path = tmp_file.name
            docs = langchain_handler.load_document(tmp_file_path)
            if docs:
                documents.extend(docs)
        except Exception as e:
            st.warning(f"Could not process file {file.filename}: {type(e).__name__}: {e}")
            continue

    if not documents:
        raise ValueError("No readable course materials available.")

    mcqs = langchain_handler.generate_mcq_questions(documents)
    return mcqs

def generate_flashcards_for_course(course):
    documents = []
    for file in course.files:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=file.filename) as tmp_file:
                tmp_file.write(file.data)
                tmp_file_path = tmp_file.name
            docs = langchain_handler.load_document(tmp_file_path)
            if docs:
                documents.extend(docs)
        except Exception as e:
            st.warning(f"Could not process file {file.filename}: {type(e).__name__}: {e}")
            continue

    if not documents:
        raise ValueError("No readable course materials available.")

    flashcards = langchain_handler.generate_flashcards(documents)
    return flashcards

def extract_text_from_pdf(pdf_file_path_or_object):
    try:
        if isinstance(pdf_file_path_or_object, str):
            pdf_reader = PyPDF2.PdfReader(pdf_file_path_or_object)
        else:
            pdf_reader = PyPDF2.PdfReader(pdf_file_path_or_object)
        text = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            extracted_text = page.extract_text()
            if extracted_text:
                text += extracted_text + "\n"
        return text
    except Exception as e:
        logging.error(f"Error extracting text from PDF: {e}")
        return ""

def navigate_to(page):
    st.session_state.page = page

def create_course_section():
    st.header("Create a New Course")
    with st.form(key='create_course_form'):
        course_name = st.text_input("Course Name")
        submit = st.form_submit_button("Create Course")
        if submit:
            if not course_name.strip():
                st.error("Course name cannot be empty.")
                return
            if session_db.query(Course).filter_by(name=course_name.strip()).first():
                st.error("A course with this name already exists.")
                return
            new_course = Course(name=course_name.strip(), professor_id=st.session_state.user.id)
            session_db.add(new_course)
            session_db.commit()
            st.success(f"Course '{course_name}' created successfully!")
            if 'courses' in st.session_state:
                st.session_state.courses.append(new_course)
            else:
                st.session_state.courses = [new_course]

def encode_video_to_base64(video_path):
    with open(video_path, "rb") as video_file:
        return base64.b64encode(video_file.read()).decode('utf-8')


def manage_courses_section():
    """
    Allows professors to manage their courses, including adding/updating YouTube links,
    uploading course materials, viewing insights, and deleting courses.
    """
    st.header("Manage Your Courses")
    courses = session_db.query(Course).filter_by(professor_id=st.session_state.user.id).all()

    if not courses:
        st.info("You have not created any courses yet.")
        return

    for course in courses:
        st.markdown(f"### {course.name}")
       
        # Section to add or update YouTube links
        with st.expander(f"Add/Update YouTube Link for {course.name}", expanded=False):
            current_link = course.youtube_link or "No link provided yet."
            st.markdown(f"**Current YouTube Link:** {current_link}")
           
            with st.form(key=f"youtube_form_{course.id}"):
                youtube_link = st.text_input("Enter YouTube Link", value=course.youtube_link or "")
                submit_link = st.form_submit_button("Save YouTube Link")
                if submit_link:
                    if youtube_link.strip():
                        course.youtube_link = youtube_link.strip()
                        session_db.commit()
                        st.success("YouTube link updated successfully!")

                        # Process transcripts and add them to vector DB
                        st.info("Processing YouTube link for transcripts...")
                        transcripts = process_youtube_links([youtube_link], course, output_dir="transcripts")

                        if transcripts:
                            # Save each transcript as a CourseFile and add to vector DB
                            for audio_file, transcript_text in transcripts.items():
                                transcript_filename = f"{os.path.splitext(audio_file)[0]}_transcript.txt"
                               
                                # Check if a transcript file with the same name already exists
                                existing_file = session_db.query(CourseFile).filter_by(
                                    course_id=course.id, filename=transcript_filename
                                ).first()

                                if existing_file:
                                    continue

                                # Save transcript to database
                                course_file = CourseFile(
                                    filename=transcript_filename,
                                    data=transcript_text.encode('utf-8'),
                                    course_id=course.id
                                )
                                session_db.add(course_file)
                               
                                # Add transcript to the vector DB
                                docs = langchain_handler.load_document(transcript_filename)
                                if docs:
                                    vector_store = langchain_handler.create_vector_store(docs)
                                    if vector_store:
                                        st.success(f"Transcript vectorized and added to the course vector DB.")
                                    else:
                                        st.error(f"Failed to vectorize transcript: {transcript_filename}")
                            session_db.commit()
                            st.success("Transcripts added as course materials!")
                        else:
                            st.warning("No transcripts were generated. Please ensure the YouTube link is correct and try again.")
                       
                    else:
                        st.error("YouTube link cannot be empty.")

        # Section to upload course materials
        with st.expander(f"Upload Course Materials for {course.name}", expanded=False):
            with st.form(key=f'upload_form_{course.id}', clear_on_submit=True):
                uploaded_files = st.file_uploader(
                    "Upload files (PDF or TXT)", accept_multiple_files=True, key=f"upload_{course.id}"
                )
                submit = st.form_submit_button("Upload Files")
                if submit:
                    if uploaded_files:
                        for uploaded_file in uploaded_files:
                            if uploaded_file.size > 10 * 1024 * 1024:
                                continue
                            existing_file = session_db.query(CourseFile).filter_by(
                                course_id=course.id, filename=uploaded_file.name
                            ).first()
                            if existing_file:
                                st.warning(f"File {uploaded_file.name} already exists and was skipped.")
                                continue
                            course_file = CourseFile(
                                filename=uploaded_file.name,
                                data=uploaded_file.read(),
                                course_id=course.id
                            )
                            session_db.add(course_file)
                        session_db.commit()
                        st.success("Files uploaded successfully!")
                        course.files = session_db.query(CourseFile).filter_by(course_id=course.id).all()
                    else:
                        st.error("No files selected.")

        # Section to toggle and display course insights
        with st.expander(f"Course Insights for {course.name}", expanded=False):
            # Initialize session state variables for this course if not already set
            if f"show_insights_{course.id}" not in st.session_state:
                st.session_state[f"show_insights_{course.id}"] = False

            # Toggle Insights Button
            toggle_label = "Hide Insights" if st.session_state[f"show_insights_{course.id}"] else "Show Insights"
            if st.button(toggle_label, key=f"toggle_insights_{course.id}"):
                st.session_state[f"show_insights_{course.id}"] = not st.session_state[f"show_insights_{course.id}"]

            # Clear Insights Button
            if st.button("Clear Insights", key=f"clear_insights_{course.id}"):
                st.session_state[f"show_insights_{course.id}"] = False

            # Display Insights if toggled on
            if st.session_state[f"show_insights_{course.id}"]:
                st.markdown("### Course Insights")
                insights_container = st.container()
                with insights_container:
                    # Define the path to your backend CSV file (assuming one per course)
                    csv_file_path = "data/ml_grouped_topics_questions.csv"  # Adjust path as needed

                    if os.path.exists(csv_file_path):
                        df = pd.read_csv(csv_file_path)
                        if 'Topic' not in df.columns or 'Question' not in df.columns:
                            st.error("CSV must have 'Topic' and 'Question' columns.")
                        else:
                            tabs = st.tabs(["📊 Pie Chart", "📈 Bar Chart", "☁️ Word Cloud", "📄 Report"])
                            with tabs[0]:
                                pie_fig = generate_pie_chart(df)
                                st.plotly_chart(pie_fig, use_container_width=True)

                            with tabs[1]:
                                bar_fig = generate_bar_chart(df)
                                st.plotly_chart(bar_fig, use_container_width=True)

                            with tabs[2]:
                                wordcloud_img = generate_wordcloud(df)
                                st.image(f"data:image/png;base64,{wordcloud_img}", use_container_width=True)

                            with tabs[3]:
                                report = generate_csv_report(csv_file_path)
                                if report.startswith("Error generating report"):
                                    st.error(report)
                                else:
                                    st.markdown(report, unsafe_allow_html=True)
                    else:
                        st.error(f"CSV file not found at the specified path: {csv_file_path}")

        # Section to delete course
        st.markdown("### Delete Course")
        with st.form(key=f'delete_course_form_{course.id}', clear_on_submit=True):
            confirm = st.checkbox(
                "Are you sure you want to delete this course? This action cannot be undone.",
                key=f"confirm_delete_{course.id}"
            )
            submit = st.form_submit_button("Delete Course")
            if submit:
                if confirm:
                    for file in course.files:
                        session_db.delete(file)
                    session_db.delete(course)
                    session_db.commit()
                    st.success(f"Course '{course.name}' deleted successfully!")
                    st.experimental_rerun()
                else:
                    st.error("Please confirm to delete the course.")



def generate_pie_chart(df):  
    print("generating pie chart")
    topic_counts = df['Topic'].value_counts().reset_index()
    topic_counts.columns = ['Topic', 'Count']
    fig = px.pie(topic_counts, names='Topic', values='Count', title='Topic Distribution',
                 hole=0.3, color_discrete_sequence=px.colors.qualitative.Pastel)
    fig.update_traces(textposition='inside', textinfo='percent+label')
    return fig

def generate_bar_chart(df):
    print("generating bar chart")
    topic_counts = df['Topic'].value_counts().sort_values(ascending=True).reset_index()
    topic_counts.columns = ['Topic', 'Count']
    fig = px.bar(topic_counts, x='Count', y='Topic', orientation='h',
                 title='Questions per Topic', color='Count', color_continuous_scale='Blues')
    fig.update_layout(showlegend=False)
    return fig

def generate_wordcloud(df):  
    print("generating word cloud")
    text = " ".join(df['Question'].dropna().tolist())
    wordcloud = WordCloud(width=800, height=400,
                          background_color='white',
                          stopwords=set(STOPWORDS),
                          min_font_size=10).generate(text)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.imshow(wordcloud, interpolation='bilinear')
    ax.axis('off')
    ax.set_title('Word Cloud', fontsize=16)
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches='tight')
    buf.seek(0)
    img_bytes = buf.read()
    encoded = base64.b64encode(img_bytes).decode()
    plt.close(fig)
    return encoded

def generate_csv_report(csv_file_path):
    try:
        df = pd.read_csv(csv_file_path)
        max_rows = 500
        if len(df) > max_rows:
            df_sample = df.sample(n=max_rows, random_state=42)
        else:
            df_sample = df

        csv_data = df_sample.to_csv(index=False)
       
        template = """
        You are a data analyst assisting a professor in understanding student questions from a course.
        Based on the following CSV data, generate a detailed and rich report that includes:

        - An overview of the total number of questions.
        - The number of unique topics covered.
        - Insights into the most common topics.
        - Any noticeable trends or patterns.
        - Suggestions for areas that may need more focus based on the questions.  
        - you are also a data analyst assisting a professor in understanding student questions from a course.
        - Based on the following CSV data, generate a detailed and rich report that includes:
        - How can we improve the course content to address these questions?

        CSV Data:
        {csv_data}

        Please present the report in a clear and organized manner, using headings and bullet points where appropriate.
        """
       
        prompt = PromptTemplate(
            input_variables=["csv_data"],
            template=template
        )
       
        llm_chain = LLMChain(llm=llm, prompt=prompt)
        report = llm_chain.run(csv_data=csv_data)
        logging.info("Generated detailed report using LLM.")
        return report.strip()
    except Exception as e:
        logging.error(f"Error generating detailed report: {str(e)}")
        return f"Error generating report: {e}"

st.markdown("""
<style>
[data-testid="stExpander"] .streamlit-expanderHeader,
[data-testid="stExpander"] .streamlit-expanderHeader * {
    color: black !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
h3#listen-to-your-podcast,
h3#listen-to-your-podcast * {
    color: black !important;
}
</style>
""", unsafe_allow_html=True)

def main():
    if "user" not in st.session_state:
        st.session_state.user = None
    if "page" not in st.session_state:
        st.session_state.page = "home"
    # Initialize popup-related states here
    if "show_chat_popup" not in st.session_state:
        st.session_state["show_chat_popup"] = False
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    page_mapping = {
        "home": home_page,
        "signup": signup_page,
        "login": login_page,
        "contact": contact_page,
        "about": about_page,
        "professor": professor_page,
        "student": student_page,
    }

    with st.container():
        col1, col2, col3, col4, col5 = st.columns(5)
        if col1.button("Home"):
            st.session_state.page = "home"
        if col2.button("Sign Up"):
            st.session_state.page = "signup"
        if col3.button("Login"):
            st.session_state.page = "login"
        if col4.button("Contact"):
            st.session_state.page = "contact"
        if col5.button("About"):
            st.session_state.page = "about"

    if st.session_state.page == "dashboard":
        if st.session_state.user and hasattr(st.session_state.user, "role"):
            if st.session_state.user.role == "professor":
                professor_page()
            else:
                student_page()
        else:
            st.error("User is not logged in or role is missing.")
    else:
        page_mapping.get(st.session_state.page, home_page)()

if __name__ == "__main__":
    main()      
