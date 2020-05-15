import os

from mongoengine import *

from dysart.labber.labber_feature import LabberFeature, result
from dysart.equs_std.fitting import spectra, rabi
from dysart.messages.messages import logged

# TODO clean up the way these are handled
template_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'templates')
qubit_rabi_file = os.path.join(template_path, 'qubit_rabi.json')
qubit_rabi_file_out = os.path.join(template_path, 'qubit_rabi_out.hdf5')
qubit_spec_file = os.path.join(template_path, 'qubit_spec.json')
qubit_spec_file_out = os.path.join(template_path, 'qubit_spec_out.hdf5')


class QubitSpectrum(LabberFeature):
    """
    Feature object for the spectrum of a qubit. For the purposes of an initial
    demo, this should really be just an abstraction of a polarization-vs-
    frequency plot.

    At the moment, there are a number of ugly hard-coded constants. As soon as
    this _works_, start fixing that first.
    """

    call_message = 'measuring qubit spectrum...'
    template_file_path = StringField(default=qubit_spec_file)
    output_file_path = StringField(default=qubit_spec_file_out)

    # Instrument names: hardcoded for now.
    pulse_generator = StringField(default='Multi-Qubit Pulse Generator - ')
    qubit_simulator = StringField(default='Single-Qubit Simulator - ')

    # Channel names: hardcoded for now.
    drive_frequency_channel = StringField(default='Single-Qubit Simulator - Drive frequency')
    polarization_Z_channel = StringField(default='Single-Qubit Simulator - Polarization - Z')

    @result
    def fit(self):
        """
        Compute a best fit for the resonance data at this index.
        """
        entry = self.log.getEntry(0)
        drive_frequency_data = entry[self.drive_frequency_channel]
        polarization_Z_data = entry[self.polarization_Z_channel]
        fit = spectra.fit_spectrum(drive_frequency_data, polarization_Z_data, 1)
        return fit.params.valuesdict()

    @result
    def center_freq(self):
        """
        Return the best guess for the resonant frequency of the qubit's 0 <-> 1
        transition
        """
        last_fit = self.fit()
        center = last_fit['_0_center']
        return center

    @result
    def linewidth(self):
        """
        Return the best guess for the HWHM (or more parameters, depending on the
        choice of fitting routine) of the qubit's 0 <-> 1 transition
        """
        last_fit = self.fit()
        fwhm = last_fit['_0_fwhm']
        return fwhm / 2


class QubitRabi(LabberFeature):
    """
    Feature object for a Rabi measurement on a qubit.
    """

    call_message = 'measuring qubit rabi'
    template_file_path = StringField(default=qubit_rabi_file)
    output_file_path = StringField(default=qubit_rabi_file_out)

    # Instrument names: hardcoded for now.
    pulse_generator = StringField(
        default='Multi-Qubit Pulse Generator - '
    )
    qubit_simulator = StringField(
        default='Single-Qubit Simulator - '
    )

    # Channel names: hardcoded for now.
    plateau_channel = StringField(
        default='Multi-Qubit Pulse Generator - Plateau'
    )
    polarization_Z_channel = StringField(
        default='Single-Qubit Simulator - Polarization - Z'
    )

    def __params__(self):
        center_freq = self.parents['spec'].center_freq()
        freq_channel = self.parents['spec'].drive_frequency_channel
        return {freq_channel: center_freq}

    @result
    def fit(self):
        """
        Compute a best fit for Rabi oscillation data at this index.
        """

        # TODO: log_history should be a member of self, not of self.results.
        # self.results should be totally hidden.

        # fit results and enter them in self.data
        entry = self.log.getEntry(0)
        plateau_data = entry[self.plateau_channel]
        polarization_Z_data = entry[self.polarization_Z_channel]
        fit = rabi.fit_rabi(plateau_data, polarization_Z_data)
        return fit.params.valuesdict()

    @result
    def frequency(self):
        """
        Return the Rabi frequency at the specified drive amplitude
        """
        last_fit = self.fit()
        return last_fit['freq']

    @result
    def pi_time(self):
        """
        Return the time to perform an X gate at the specified drive amplitude
        """
        rabi_period = 1 / self.frequency()
        return rabi_period / 2

    @result
    def pi_2_time(self):
        """
        Return the time to perform an H gate at the specified drive amplitude
        """
        rabi_period = 1 / self.frequency()
        return rabi_period / 4

    @result
    def decay_rate(self):
        """
        Return the rate at which Rabi oscillations damp out
        """
        last_fit = self.fit()
        return last_fit['decay']

    @result
    def decay_time(self):
        """
        Return the Rabi decay time (or more parameters, depending on choice of
        fitting routine)
        """
        return 1 / self.decay_rate()

    @result
    def phase(self):
        """
        Return the Rabi phase. Note that this can be nonzero, depending on the
        drive pulse shape.
        """
        last_fit = self.fit()
        return last_fit['phase']


class QubitRelaxation(LabberFeature):
    """
    Feature object for a T1 measurement on a qubit.
    (Actually, it might make sense for the same Feature class to track all the
    relaxation data. TBD.)
    """

    data = DictField(default={'fit_results': []})
    input_file = StringField(default='')
    output_file = StringField(default='')

    @logged(message='measuring qubit relaxation', end='\n')
    def __call__(self, **kwargs):
        # TODO: other stuff
        super().__call__(self)
        # TODO: other stuff

    @result
    def time_const(self):
        """
        Return the time for qubit polarization to decay to 1/e of its original
        value
        """
        # TODO
        pass
