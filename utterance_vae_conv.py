import theano.tensor as T
import numpy as np
import config
import feedforward
from theano_toolkit import utils as U
import vae


activation_map = {
    "softplus": T.nnet.softplus,
    "sigmoid": T.nnet.sigmoid,
    "tanh": T.tanh,
    "relu": T.nnet.relu
}

initalisation_map = {
    "softplus": feedforward.relu_init,
    "relu": feedforward.relu_init,
    "tanh": feedforward.initial_weights,
    "sigmoid": feedforward.initial_weights,
}

activation_function = config.option("activation_function",
                                    "Activation function.",
                                    default="softplus")


@activation_function
def activation(x, activation_function):
    return activation_map[activation_function](x)


@activation_function
def weight_init(x, y, activation_function):
    return initalisation_map[activation_function](x, y)


input_dimension = config.option(
    "input_dimension", "Size of input.", type=config.int)
shared_structure = config.option(
    "shared_structure", "Structure for shared portion.", type=config.structure)
acoustic_structure = config.option(
    "acoustic_structure", "Structure for acoustic portion.",
    type=config.structure)
speaker_structure = config.option(
    "speaker_structure", "Structure for speaker portion.",
    type=config.structure)
pooling_method = config.option(
    "pooling_method", "Method for pooling over utterance.")


@pooling_method
def build_speaker_inferer(P, input_dimension, layer_size, speaker_latent_size,
                          pooling_method):

    input_transform = feedforward.build_transform(
        P, name="speaker_encoder",
        input_size=input_dimension,
        output_size=layer_size,
        initial_weights=weight_init,
        activation=activation,
        batch_norm=False
    )
    assert(pooling_method == "max")
    P.W_speaker_encoder_pool = weight_init(layer_size, layer_size)
    P.b_speaker_encoder_pool = np.zeros((layer_size,), dtype=np.float32)

    output = vae.build_inferer(
        P, name="speaker_encoder_pooled",
        input_sizes=[layer_size],
        hidden_sizes=[layer_size],
        output_size=speaker_latent_size,
        initial_weights=weight_init,
        activation=activation,
        initialise_outputs=weight_init
    )

    def speaker_encode(X, mask):
        mask = mask[:, :, None]
        hidden = input_transform(X)
        features = (T.dot(hidden, P.W_speaker_encoder_pool) +
                    P.b_speaker_encoder_pool)
        pooled = T.max(T.switch(mask, features, -np.inf), axis=1)
        return output([pooled])
    return speaker_encode

if False:
    def build_conv_transform(P, name, input_size, output_size, context=5):
        P['W_%s_conv' % name] = weight_init(input_size, output_size)
        P['b_%s_conv' % name] = np.zeros((output_size,))
        W = P['W_%s_conv' % name]
        b = P['b_%s_conv' % name]

        def conv(x, mask):
            output = activation(T.dot(x, W) + b)
            output = T.switch(mask[:, :, None], output, 0)
            return output
        return conv
else:
    def build_conv_transform(P, name, input_size, output_size, context=5,
                             weight_init=weight_init,
                             activation=activation):
        W_val = weight_init(
            input_size * (2 * context + 1),
            output_size
        ).reshape((output_size, input_size, 1, 2 * context + 1))
        P['W_%s_conv' % name] = W_val
        P['b_%s_conv' % name] = np.zeros((output_size,))
        W = P['W_%s_conv' % name]
        b = P['b_%s_conv' % name].dimshuffle('x', 0, 'x', 'x')

        def conv(x, mask):
            # batch_size x sequence_length x hidden_size
            mask = mask[:, :, None]
            x = T.switch(mask, x, 0)
            x = x.dimshuffle(0, 2, 'x', 1)
            # batch_size x hidden_size x 1 x sequence_length
            conv_out_ = T.nnet.conv2d(
                x, W,
                border_mode='half'
            ) + b
            output = activation(conv_out_)
            output = output.dimshuffle(2, 0, 3, 1)[0]
            output = T.switch(mask, output, 0)
            return output
        return conv


@input_dimension
@shared_structure
@acoustic_structure
@speaker_structure
def build_encoder(P, input_dimension, shared_structure, acoustic_structure,
                  speaker_structure):
    acoustic_latent_size = acoustic_structure[-1]
    speaker_latent_size = speaker_structure[-1]

    shared_transform = build_conv_transform(
        P, name="encoder",
        input_size=input_dimension,
        output_size=shared_structure[-1],
        context=5
    )

    shared2speaker = build_speaker_inferer(
        P,
        input_dimension=shared_structure[-1],
        layer_size=speaker_structure[-1],
        speaker_latent_size=speaker_latent_size,
    )

    shared2acoustic = vae.build_inferer(
        P, name="acoustic_encoder",
        input_sizes=[shared_structure[-1], speaker_latent_size],
        hidden_sizes=acoustic_structure[:-1],
        output_size=acoustic_latent_size,
        initial_weights=weight_init,
        activation=activation,
        initialise_outputs=weight_init
    )

    def speaker_encode(X, mask):
        shared_rep = shared_transform(X, mask)
        return shared2speaker(shared_rep, mask)

    def acoustic_encode(X, utterance_speaker, mask):
        shared_rep = shared_transform(X, mask)
        return shared2acoustic([shared_rep, utterance_speaker])

    return speaker_encode, acoustic_encode


@input_dimension
@acoustic_structure
@speaker_structure
@config.option("decoder_structure", "Structure for decoder.",
               type=config.structure)
def build(P, input_dimension, acoustic_structure, speaker_structure,
          decoder_structure):
    decoder_layer_sizes = decoder_structure
    acoustic_latent_size = acoustic_structure[-1]
    speaker_latent_size = speaker_structure[-1]

    speaker_encode, acoustic_encode = build_encoder(P)

    decode = feedforward.build_classifier(
        P, name="decode",
        input_sizes=[acoustic_latent_size,
                     speaker_latent_size],
        hidden_sizes=decoder_layer_sizes,
        output_size=decoder_layer_sizes[-1],
        initial_weights=weight_init,
        activation=activation,
        output_activation=activation,
        output_weights=weight_init,
    )

    decode_conv = build_conv_transform(
        P, name="decoder",
        input_size=decoder_layer_sizes[-1],
        output_size=input_dimension * 2,
        weight_init=lambda x, y: np.zeros((x, y)),
        activation=lambda x: x,
        context=5
    )

    def unsupervised_training_cost(X, utt_lengths):
        # X: batch_size, sequence_length, input_size
        # utt_lengths: batch_size
        mask = T.arange(X.shape[1])[None, :] < utt_lengths[:, None]

        # Get latent variables
        (_,
         utterance_speaker_mean,
         utterance_speaker_std) = speaker_encode(X, mask)
        utterance_speaker = (
            utterance_speaker_mean[:, None, :] + (
                utterance_speaker_std[:, None, :] *
                U.theano_rng.normal(size=(
                    utterance_speaker_std.shape[0],
                    X.shape[1],
                    utterance_speaker_std.shape[1]
                ))
            )
        )
        acoustic, acoustic_mean, acoustic_std = acoustic_encode(
            X, utterance_speaker, mask
        )
        acoustic = T.switch(mask[:, :, None], acoustic, 0)
        # Combine distributions for utterance

        # Reconstruct
        _, final_hidden = decode([acoustic, utterance_speaker])
        #_, pre_conv_hidden = decode([acoustic, utterance_speaker])
        #final_hidden = decode_conv(pre_conv_hidden, mask)
        #_, recon_X_mean, recon_X_std = decoder_output(final_hidden)
        #recon_X_mean = T.switch(mask[:, :, None], recon_X_mean, 0)
        #recon_X_std = T.switch(mask[:, :, None], recon_X_std, 1)

        recon_X_mean_std = decode_conv(final_hidden, mask)

        recon_X_mean = T.switch(
            mask[:, :, None],
            recon_X_mean_std[:, :, :input_dimension],
            0
        )
        recon_X_std = T.switch(
            mask[:, :, None],
            T.nnet.softplus(
                recon_X_mean_std[:, :, input_dimension:] +
                np.log(np.exp(1) - 1)
            ),
            1
        )


        acoustic_latent_cost = vae.kl_divergence(
            acoustic_mean, acoustic_std,
            0, 1
        )  # batch_size, sequence_length
        speaker_latent_cost = vae.kl_divergence(
            utterance_speaker_mean, utterance_speaker_std,
            0, 1
        )  # batch_size
        recon_cost = vae.gaussian_nll(X, recon_X_mean, recon_X_std)

        batch_speaker_latent_cost = speaker_latent_cost
        batch_acoustic_latent_cost = T.sum(
            T.switch(mask, acoustic_latent_cost, 0), axis=1)
        batch_reconstruction_cost = T.sum(
            T.switch(mask, recon_cost, 0), axis=1)

        return batch_speaker_latent_cost,\
            batch_acoustic_latent_cost,\
            batch_reconstruction_cost

    return unsupervised_training_cost