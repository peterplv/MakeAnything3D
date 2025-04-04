import os
import subprocess
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Value
import cv2
import torch
import numpy as np

from depth_anything_v2.dpt import DepthAnythingV2


# GENERAL OPTIONS
# Folder with source frames
frames_dir = ""

# Get the name of the source frames folder to create a folder for 3D frames
frames_dir_name = os.path.basename(os.path.normpath(frames_dir))
images3d_dir = os.path.join(os.path.dirname(frames_dir), f"{frames_dir_name}_3d")
os.makedirs(images3d_dir, exist_ok=True)

# Get a list of all files in the directory
all_frames_in_directory = [file_name for file_name in os.listdir(frames_dir) if os.path.isfile(os.path.join(frames_dir, file_name))]

frame_counter = Value('i', 0) # Counter for naming frames
threads_count = Value('i', 0) # Current threads counter to stay within max_threads limits

chunk_size = 1000  # Number of files per thread
max_threads = 3 # Maximum streams

# Computing device
device = torch.device('cuda')


''' 3D OPTIONS COMMENTS
## PARALLAX_SCALE:
Parallax value in pixels, by how many maximum pixels the far pixels will be shifted relative to the near pixels.
Recommended from 10 to 20.

## INTERPOLATION_TYPE:
INTER_NEAREST – Nearest-neighbor interpolation. Fastest but lowest quality (pixelated edges).
INTER_AREA – Best for image reduction (averaging pixels). Not considered in this case.
INTER_LINEAR – Bilinear interpolation. Balanced quality and speed (recommended default).
INTER_CUBIC – Bicubic interpolation (4×4 pixel neighborhood). Higher quality than linear but slower.
INTER_LANCZOS4 – Lanczos interpolation (8×8 pixel neighborhood). Highest quality but significantly slower.

## TYPE3D:
HSBS (Half Side-by-Side) - half horizontal stereopair
FSBS (Full Side-by-Side) - full horizontal stereopair
HOU (Half Over-Under) - half vertical stereopair
FOU (Full Over-Under) - full vertical stereopair

## LEFT_RIGHT:
The order of a pair of frames in the overall 3D image, LEFT is left first, RIGHT is right first.

## new_width + new_height:
Change the resolution of the output image without warping (with black margins added).
If there's no need to change, then new_width = 0 and new_height = 0
'''

# 3D PARAMETERS
PARALLAX_SCALE = 15  # Recommended 10 to 20
INTERPOLATION_TYPE = cv2.INTER_LINEAR
TYPE3D = "FSBS"  # HSBS, FSBS, HOU, FOU
LEFT_RIGHT = "LEFT"  # LEFT or RIGHT

# 0 - if there's no need to change frame size
new_width = 0
new_height = 0

# Path to the folder with models, specify without a slash at the end, for example: "/home/user/DepthAnythingV2/models"
depth_model_dir = ""

model_depth_configs = {
        'vits': {'encoder': 'vits', 'features': 64, 'out_channels': [48, 96, 192, 384]},
        'vitb': {'encoder': 'vitb', 'features': 128, 'out_channels': [96, 192, 384, 768]},
        'vitl': {'encoder': 'vitl', 'features': 256, 'out_channels': [256, 512, 1024, 1024]}
}

encoder = 'vitl' # 'vitl', 'vitb', 'vits'

model_depth = DepthAnythingV2(**model_depth_configs[encoder])
model_depth.load_state_dict(torch.load(f'{depth_model_dir}/depth_anything_v2_{encoder}.pth', weights_only=True, map_location=device))
model_depth = model_depth.to(device).eval()


def image_size_correction(current_height, current_width, left_image, right_image):
    ''' Image size correction if new_width and new_height are set '''
    
    # Calculate offsets for centering
    top = (new_height - current_height) // 2
    bottom = new_height - current_height - top
    left = (new_width - current_width) // 2
    right = new_width - current_width - left
    
    # Create a black canvas of the desired size
    new_left_image = np.zeros((new_height, new_width, 3), dtype=np.uint8)
    new_right_image = np.zeros((new_height, new_width, 3), dtype=np.uint8)
    
    # Placing the image on a black background
    new_left_image[top:top + current_height, left:left + current_width] = left_image
    new_right_image[top:top + current_height, left:left + current_width] = right_image
    
    return new_left_image, new_right_image
            
def depth_processing(frame_name, frame_path):
    ''' Function for creating a depth map for an image '''
    
    # Loading the image
    raw_img = cv2.imread(frame_path)

    # Depth calculation
    with torch.no_grad():
        depth = model_depth.infer_image(raw_img)
        
    # Normalization of depth values from 0 to 255
    depth_normalized = cv2.normalize(depth, None, 0, 255, norm_type=cv2.NORM_MINMAX)
    depth_normalized = depth_normalized.astype(np.uint8)

    return depth_normalized

def image3d_processing(frame_name, frame_path, depth):
    ''' 3D creation function based on the original image and its depth map '''
    
    # Loading the image
    image = cv2.imread(frame_path)
    
    # If the sizes of the original frame and the depth map do not match, the depth is scaled to the image size
    if image.shape[:2] != depth.shape[:2]:
        depth = cv2.resize(depth, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR)
    
    # Depth normalization
    depth = depth.astype(np.float32) / 255.0

    # Creating parallax
    height, width, _ = image.shape
    parallax = (depth * PARALLAX_SCALE)

    # Pixel coordinates
    x, y = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))

    # Calculation of offsets
    shift_left = np.clip(x + parallax.astype(np.float32), 0, width - 1)
    shift_right = np.clip(x - parallax.astype(np.float32), 0, width - 1)

    # Applying offsets with cv2.remap
    left_image = cv2.remap(image, shift_left, y, interpolation=INTERPOLATION_TYPE)
    right_image = cv2.remap(image, shift_right, y, interpolation=INTERPOLATION_TYPE)
    
    if new_width != 0 and new_height != 0:
        left_image, right_image = image_size_correction(height, width, left_image, right_image)
        # Change the values of the original image sizes to new_height and new_width for correct gluing below
        height = new_height
        width = new_width
    
    # Combine left and right images into a common 3D image
    if TYPE3D == "HSBS":
        # Narrowing the width of images to make them into one with a common width
        left_image_resized = cv2.resize(left_image, (width // 2, height), interpolation=cv2.INTER_AREA)
        right_image_resized = cv2.resize(right_image, (width // 2, height), interpolation=cv2.INTER_AREA)
        # Merge images into one
        if LEFT_RIGHT == "LEFT":
            image3d = np.hstack((left_image_resized, right_image_resized))
        elif LEFT_RIGHT == "RIGHT":
            image3d = np.hstack((right_image_resized, left_image_resized))
    elif TYPE3D == "HOU":
        # Narrowing the height of images to make them into one with a common height
        left_image_resized = cv2.resize(left_image, (width, height // 2), interpolation=cv2.INTER_AREA)
        right_image_resized = cv2.resize(right_image, (width, height // 2), interpolation=cv2.INTER_AREA)
        # Merge images into one
        if LEFT_RIGHT == "LEFT":
            image3d = np.vstack((left_image_resized, right_image_resized))
        elif LEFT_RIGHT == "RIGHT":
            image3d = np.vstack((right_image_resized, left_image_resized))
    elif TYPE3D == "FSBS":
        # Merge images into one
        if LEFT_RIGHT == "LEFT":
            image3d = np.hstack((left_image, right_image))
        elif LEFT_RIGHT == "RIGHT":
            image3d = np.hstack((right_image, left_image))
    elif TYPE3D == "FOU":
        # Merge images into one
        if LEFT_RIGHT == "LEFT":
            image3d = np.vstack((left_image, right_image))
        elif LEFT_RIGHT == "RIGHT":
            image3d = np.vstack((right_image, left_image))

    # Saving 3D image
    output_image3d_path = os.path.join(images3d_dir, f'{frame_name}.jpg')
    cv2.imwrite(output_image3d_path, image3d, [int(cv2.IMWRITE_JPEG_QUALITY), 100])
    
def extract_frames(start_frame, end_frame):
    ''' Function for chunking image files based on chunk_size '''
    
    frames_to_process = end_frame - start_frame + 1
    
    with frame_counter.get_lock():
        start_counter = frame_counter.value
        frame_counter.value += frames_to_process
        
    extracted_frames = []  # List to store file paths
    
    # Get a dictionary with a list of files in the chunk size
    chunk_files = all_frames_in_directory[start_frame:end_frame+1]  # end_frame inclusive
    extracted_frames = [os.path.join(frames_dir, file_name) for file_name in chunk_files]
    
    return extracted_frames

def chunk_processing(extracted_frames):
    ''' Start processing of each filled chunk '''
    
    for frame_path in extracted_frames:
        # Check that it's a file and not a folder
        if not os.path.isfile(frame_path):
            continue
        
        # Extract the image name to save the 3D image later on
        frame_name = os.path.splitext(os.path.basename(frame_path))[0]

        # Runing depth_processing
        depth = depth_processing(frame_name, frame_path)

        # Runing image3d_processing and saving the result
        image3d_processing(frame_name, frame_path, depth)

        # Deleting the source file
        os.remove(frame_path)
        
    with threads_count.get_lock():
        threads_count.value = max(1, threads_count.value - 1) # Decrease the counter after the current thread is finished
    
def run_processing():
    ''' Global function of processing start taking into account multithreading '''
    
    # Getting the number of files in the source folder
    total_frames = len([f for f in os.listdir(frames_dir) if os.path.isfile(os.path.join(frames_dir, f))])
                        
    # Threads control
    if total_frames:
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = []
            for start_frame in range(0, total_frames, chunk_size):
                while True:
                    with threads_count.get_lock():
                        if threads_count.value < max_threads:
                            threads_count.value += 1
                            break
                            
                    time.sleep(5) # Pause before rechecking for the number of running threads

                end_frame = min(start_frame + chunk_size - 1, total_frames - 1)
                extracted_frames = extract_frames(start_frame, end_frame)
                future = executor.submit(chunk_processing, extracted_frames)
                futures.append(future)
            
            # Waiting for tasks to complete
            for future in futures:
                future.result()


# START PROCESSING
run_processing()

print("DONE.")


# Delete the model from memory and clear the Cuda cache
del model_depth
torch.cuda.empty_cache()