from PIL import Image

# Open the JPG image
img = Image.open('icon.jpg')

# Convert and save as ICO with multiple sizes for the best visual quality
# This creates a multi-resolution icon file
img.save('icon.ico', format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

print("Successfully converted icon.jpg to icon.ico")