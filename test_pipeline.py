import os
import time
import tempfile
from config import Config
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner

def main() -> None:
    print("==================================================")
    print("         Personal Dictation Pipeline Test         ")
    print("==================================================")
    
    # 1. Initialize models
    print("Initializing local Whisper Transcriber...")
    transcriber = WhisperTranscriber()
    
    print("Initializing Text Refiner Client...")
    refiner = TextRefiner()
    
    temp_dir: str = tempfile.gettempdir()
    audio_path: str = os.path.join(temp_dir, "test_dictation.wav")
    
    # 2. Recording Test
    recorder = AudioRecorder(sample_rate=Config.SAMPLE_RATE, channels=Config.CHANNELS)
    print("\n--- Audio Recording Test (5 Seconds) ---")
    print("Prepare to speak (e.g. 'hello this is a test') after the beep...")
    time.sleep(1.0)
    
    # Play start cue
    if Config.PLAY_AUDIO_CUES:
        play_beep(880.0, 0.1)
        
    recorder.start()
    print("Recording... Speak now!")
    
    # Record for 5 seconds
    time.sleep(5.0)
    
    # Play stop cue
    if Config.PLAY_AUDIO_CUES:
        play_beep(440.0, 0.1)
        
    success: bool = recorder.stop(audio_path)
    print("Recording stopped.")
    
    if not success or not os.path.exists(audio_path):
        print("Error: Audio recording failed or file was not saved.")
        return
        
    # 3. Transcription Test
    print("\n--- Whisper Transcription Test ---")
    try:
        start_t: float = time.time()
        raw_text: str = transcriber.transcribe(audio_path)
        elapsed_stt: float = time.time() - start_t
        print(f"STT Time:       {elapsed_stt:.2f} seconds")
        print(f"Raw Transcript: \"{raw_text}\"")
    except Exception as e:
        print(f"Error during transcription: {e}")
        raw_text = ""
        
    # 4. Refinement Test
    if raw_text:
        print("\n--- LLM Refinement Test ---")
        try:
            start_t = time.time()
            refined_text: str = refiner.refine(raw_text)
            elapsed_llm: float = time.time() - start_t
            print(f"LLM Time:       {elapsed_llm:.2f} seconds")
            print(f"Refined Text:   \"{refined_text}\"")
        except Exception as e:
            print(f"Error during LLM refinement: {e}")
    else:
        print("\nSkipping refinement because transcription was empty.")
        
    # Cleanup
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
            print("\nTemporary test recording cleaned up successfully.")
        except Exception as e:
            print(f"Warning: Failed to delete temporary test audio file: {e}")
            
    print("==================================================")
    print("               Pipeline Test Finished             ")
    print("==================================================")

if __name__ == "__main__":
    main()
