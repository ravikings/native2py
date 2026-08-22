!=======================================================================
! PETRO_API - thin Fortran 90 facade over the legacy PVTCOR/HYDRAU/WELLIB
!             fixed-form decks.
!
! Added 12-MAR-2003 so that the (then new) VB6 desktop tool could call
! the correlation library without linking against the whole simulator.
! It is the ONLY part of libraries/petro with explicit interfaces,
! intent attributes and no COMMON in the argument path -- everything
! else still communicates through /FLUID/ and /PVTOUT/.
!
! Every routine here is a wrapper. The real work is still in the .f
! decks; this file only marshals arguments and copies results out of
! the COMMON blocks so callers do not have to know they exist.
!
! NOTE: the hidden state is still global. Calling pvt_set_fluid from
! two threads will corrupt /FLUID/. The desktop tool was single
! threaded so this was never fixed.
!=======================================================================
module petro_api

    implicit none

    integer, parameter :: dp = kind(1.0d0)

contains

    !-------------------------------------------------------------------
    ! Initialise the fluid description. Must be called before anything
    ! else in this module. Wraps PVTINI.
    !-------------------------------------------------------------------
    subroutine pvt_set_fluid(api_gravity, gas_gravity, temp_f, icorr)
        real(8), intent(in) :: api_gravity
        real(8), intent(in) :: gas_gravity
        real(8), intent(in) :: temp_f
        integer,  intent(in) :: icorr

        call pvtini(api_gravity, gas_gravity, temp_f, icorr)
    end subroutine pvt_set_fluid

    !-------------------------------------------------------------------
    ! Solution gas oil ratio, scf/stb.
    !-------------------------------------------------------------------
    function solution_gor(pressure) result(rs)
        real(8), intent(in) :: pressure
        real(8) :: rs
        real(8) :: pvtrs
        external :: pvtrs

        rs = pvtrs(pressure)
    end function solution_gor

    !-------------------------------------------------------------------
    ! Oil formation volume factor, rb/stb.
    !-------------------------------------------------------------------
    function oil_fvf(pressure) result(bo)
        real(8), intent(in) :: pressure
        real(8) :: bo
        real(8) :: pvtbo
        external :: pvtbo

        bo = pvtbo(pressure)
    end function oil_fvf

    !-------------------------------------------------------------------
    ! Live oil viscosity, cp.
    !-------------------------------------------------------------------
    function oil_viscosity(pressure) result(mu)
        real(8), intent(in) :: pressure
        real(8) :: mu
        real(8) :: pvtvis
        external :: pvtvis

        mu = pvtvis(pressure)
    end function oil_viscosity

    !-------------------------------------------------------------------
    ! Gas deviation factor, dimensionless.
    !-------------------------------------------------------------------
    function gas_z_factor(pressure) result(z)
        real(8), intent(in) :: pressure
        real(8) :: z
        real(8) :: pvtz
        external :: pvtz

        z = pvtz(pressure)
    end function gas_z_factor

    !-------------------------------------------------------------------
    ! Bubble point pressure, psia, for a target solution GOR.
    !-------------------------------------------------------------------
    function bubble_point(target_gor) result(pb)
        real(8), intent(in) :: target_gor
        real(8) :: pb
        real(8) :: pvtbub
        external :: pvtbub

        pb = pvtbub(target_gor)
    end function bubble_point

    !-------------------------------------------------------------------
    ! Complete PVT state at one pressure, returned as an array so the
    ! caller never touches /PVTOUT/ directly.
    !
    !   props(1) = Bo    props(2) = Bg    props(3) = Bw
    !   props(4) = Rs    props(5) = muo   props(6) = mug
    !   props(7) = rhoo  props(8) = rhog  props(9) = Z
    !-------------------------------------------------------------------
    subroutine pvt_state(pressure, props, n)
        real(8), intent(in)    :: pressure
        real(8), intent(inout) :: props(n)
        integer,  intent(in)    :: n

        real(8) :: bo, bg, bw, rsol, viso, visg, visw
        real(8) :: rhoo, rhog, rhow, zfac, co, cg, cw
        common /pvtout/ bo, bg, bw, rsol, viso, visg, visw, &
                        rhoo, rhog, rhow, zfac, co, cg, cw

        if (n < 9) return

        call pvtset(pressure)

        props(1) = bo
        props(2) = bg
        props(3) = bw
        props(4) = rsol
        props(5) = viso
        props(6) = visg
        props(7) = rhoo
        props(8) = rhog
        props(9) = zfac
    end subroutine pvt_state

    !-------------------------------------------------------------------
    ! Bottomhole pressure from a Beggs and Brill traverse, psia.
    ! Wraps TRAVER.
    !-------------------------------------------------------------------
    function tubing_bhp(wellhead_p, q_oil, q_water, q_gas, diameter, &
                        roughness, tvd, md, nseg) result(pbh)
        real(8), intent(in) :: wellhead_p
        real(8), intent(in) :: q_oil
        real(8), intent(in) :: q_water
        real(8), intent(in) :: q_gas
        real(8), intent(in) :: diameter
        real(8), intent(in) :: roughness
        real(8), intent(in) :: tvd
        real(8), intent(in) :: md
        integer,  intent(in) :: nseg
        real(8) :: pbh

        integer :: nlocal

        nlocal = nseg
        call traver(wellhead_p, q_oil, q_water, q_gas, diameter, &
                    roughness, tvd, md, nlocal, pbh)
    end function tubing_bhp

    !-------------------------------------------------------------------
    ! Vogel inflow performance, stb/d.
    !-------------------------------------------------------------------
    function vogel_rate(res_pressure, flowing_bhp, q_max) result(q)
        real(8), intent(in) :: res_pressure
        real(8), intent(in) :: flowing_bhp
        real(8), intent(in) :: q_max
        real(8) :: q
        real(8) :: iprvog
        external :: iprvog

        q = iprvog(res_pressure, flowing_bhp, q_max)
    end function vogel_rate

    !-------------------------------------------------------------------
    ! Last error code raised by the legacy layer. Clears the state.
    !-------------------------------------------------------------------
    function last_error() result(icode)
        integer :: icode
        character(len=64) :: msg

        call pvterr(icode, msg)
    end function last_error

end module petro_api
