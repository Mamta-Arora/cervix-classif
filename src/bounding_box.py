from collections import OrderedDict
from os import listdir
from os.path import join, splitext

import fire
import re
import ijroi
import numpy as np
from keras.applications import Xception
from keras.callbacks import ReduceLROnPlateau, ModelCheckpoint
from keras.layers import Dense, Dropout
from keras.models import Sequential, Model
from keras.preprocessing.image import ImageDataGenerator, load_img, \
    img_to_array
from keras.regularizers import l2

from xception_fine_tune import HEIGHT, WIDTH
from data_provider import MODELS_DIR, load_organized_data_info
from xception_fine_tune import _top_classifier

IJ_ROI_DIR = join('data', 'bounding_boxes_299')
MODEL_FILE = join(MODELS_DIR, 'localizer.h5')

CLASSES = ['Type_1', 'Type_2', 'Type_3']
TRAINING_DIR = join('data', 'train_299')

__all__ = ['number_tagged', 'train', 'predict']


def _get_dict_roi(directory=None):
    """Get all available images with ROI bounding box.
    
    Returns
    -------
    dict : {<image_id>: <ROI file path>}
    
    """
    d = OrderedDict()
    for f in listdir(directory or IJ_ROI_DIR):
        d[splitext(f)[0]] = join(directory or IJ_ROI_DIR, f)
    return d


def _get_dict_all_images(directory=None):
    """Get all available images witch have an ROI bounding box label.
    
    Returns
    -------
    dict : {<image_id>: <image file path>}
    
    """
    d = OrderedDict()
    id_pattern = re.compile(r'\d+')
    for class_ in CLASSES:
        for f in listdir(join(directory or TRAINING_DIR, class_)):
            img_id = splitext(f)[0]
            # Because the clened images sometimes contain other information
            # e.g. additional or train, we want to extract only the id, so we
            # match on that.
            img_id = id_pattern.search(img_id).group(0)
            d[img_id] = join(directory or TRAINING_DIR, class_, f)
    return d


def _get_dict_tagged_images(directory=None, roi_directory=None):
    """Get all available images in the training directory.
    
    Returns
    -------
    dict : {<image_id>: <image file path>}
    
    """
    all_images = _get_dict_all_images(directory)
    tagged_roi = _get_dict_roi(roi_directory)
    d = OrderedDict()
    for img_id in all_images:
        if img_id in tagged_roi:
            d[img_id] = all_images[img_id]
    return d


def _get_dict_untagged_images(directory=None):
    d = _get_dict_all_images(directory)
    for img_id in _get_dict_tagged_images(directory):
        del d[img_id]
    return d


def _convert_from_roi(fname):
    """Convert a roi file to a numpy array [x, y, h, w].

    Parameters
    ----------
    fname : string
        If ends with `.roi`, we assume a full path is given

    """
    if not fname.endswith('.roi'):
        fname = '%s.roi' % join(IJ_ROI_DIR, fname)

    with open(fname, 'rb') as f:
        roi = ijroi.read_roi(f)
        top, left = roi[0]
        bottom, right = roi[2]
        height, width = bottom - top, right - left

        return np.array([top, left, height, width])


def _get_tagged_images(training_dir, roi_dir=None):
    """Read images, tags and labels for any images that have been tagged.

    Return
    ------
    labels : array
    X : np.array
        Images
    Y : np.array
        Bounding boxes in format [y, x, h, w]

    """
    roi_dict = _get_dict_roi(roi_dir or IJ_ROI_DIR)
    img_dict = _get_dict_tagged_images(training_dir, roi_dir)
    # Initialize X and Y (contains 4 values x, y, w, h)
    X = np.zeros((len(img_dict), HEIGHT, WIDTH, 3))
    Y = np.zeros((len(img_dict), 4))
    # Load the image files into a nice data array
    for idx, key in enumerate(img_dict):
        img = load_img(img_dict[key], target_size=(HEIGHT, WIDTH))
        X[idx] = img_to_array(img)
        Y[idx] = _convert_from_roi(roi_dict[key])

    return list(img_dict.keys()), X, Y


def _load_images(fnames):
    X = np.zeros((len(fnames), HEIGHT, WIDTH, 3))
    for idx, fname in enumerate(fnames):
        X[idx] = load_img(join(TRAINING_DIR, fname))
    return fnames, X


def _get_untagged_images():
    img_dict = _get_dict_untagged_images()
    X = np.zeros((len(img_dict), HEIGHT, WIDTH, 3))
    for idx, img_id in enumerate(img_dict):
        X[idx] = load_img(img_dict[img_id])
    return list(img_dict.keys()), X


def _get_all_images():
    img_dict = _get_dict_all_images()
    X = np.zeros((len(img_dict), HEIGHT, WIDTH, 3))
    for idx, img_id in enumerate(img_dict):
        X[idx] = load_img(img_dict[img_id])
    return list(img_dict.keys()), X


def number_tagged():
    print('Number of tagged images', _get_tagged_images()[1].shape[0])
    print('Number of untagged images', _get_untagged_images()[1].shape[0])


def _cnn(model_file):
    # Load the classification model to get the trianed weights
    model = Xception(weights='imagenet', include_top=False, pooling='avg')
    top_classifier = _top_classifier(
        l2_reg=0,
        dropout_p=0.5,
        input_shape=(2048,)
    )
    model_ = Model(inputs=model.input, outputs=top_classifier(model.output))
    model_.load_weights(model_file)
    # Time to chop off the classification head and attach the regression head
    regression_head = _regression_head(
        l2_reg=0.0,
        dropout_p=0.5,
        input_shape=(2048,),
    )
    return Model(inputs=model.input, outputs=regression_head(model.output))


def _regression_head(l2_reg, dropout_p, input_shape):
    model = Sequential()
    model.add(Dropout(rate=dropout_p, input_shape=input_shape))
    dense = Dense(
        units=4,
        kernel_regularizer=l2(l=l2_reg),
    )
    model.add(dense)
    return model


def train(model_file, reduce_lr_factor=1e-1, num_freeze_layers=0, epochs=10,
          name=''):
    data_info = load_organized_data_info(imgs_dim=HEIGHT, name=name)
    _, X_tr, Y_tr = _get_tagged_images(data_info['dir_tr'])
    _, X_val, Y_val = _get_tagged_images(data_info['dir_val'])

    def _image_generator(data, labels):
        return generator.flow(
            data, labels,
            batch_size=32,
            shuffle=True,
        )

    model = _cnn(model_file)
    # TODO See if an L1 loss does any better
    model.compile(loss='mean_squared_error', optimizer='adam')

    # model has 134 layers
    for layer in model.layers[:num_freeze_layers]:
        layer.trainable = False

    generator = ImageDataGenerator()
    callbacks = [
        ReduceLROnPlateau(factor=reduce_lr_factor),
        ModelCheckpoint(MODEL_FILE, save_best_only=True),
    ]
    model.fit_generator(
        generator=_image_generator(X_tr, Y_tr),
        steps_per_epoch=len(X_tr),
        epochs=epochs,
        callbacks=callbacks,
        validation_data=_image_generator(X_val, Y_val),
        validation_steps=len(X_val),
    )


def predict():
    model = _cnn()
    model.load_weights(MODEL_FILE)
    labels, X = _get_all_images()

    print(labels[:20])
    predictions = model.predict(X)

    print(predictions[:5])
    print(predictions.shape)

    np.save('predictions.npy', predictions)


if __name__ == '__main__':
    fire.Fire()
